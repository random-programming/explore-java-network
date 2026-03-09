package benchmark.server.iouring.ffm.mt;

import java.lang.foreign.*;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;

import static benchmark.server.iouring.ffm.mt.IoUringNative.*;

/**
 * Worker thread that owns its own io_uring ring.
 * Processes recv/send/close operations for connections assigned by the acceptor.
 */
public class WorkerThread extends Thread {

    // user_data encoding: high 8 bits = operation type, low 56 bits = fd
    static final long UD_RECV  = 2L << 56;
    static final long UD_SEND  = 3L << 56;
    static final long UD_CLOSE = 4L << 56;
    static final long UD_FD_MASK = (1L << 56) - 1;

    private static final int RING_ENTRIES = 4096;
    private static final int FIXED_BUF_COUNT = 2048;
    private static final int FIXED_BUF_SIZE = 4096;
    private static final boolean USE_SQPOLL = true;
    private static final int SQPOLL_IDLE_MS = 1000;

    private final int workerId;
    private final Map<Integer, MemorySegment> closeResponses;
    private final Map<Integer, MemorySegment> keepAliveResponses;
    private final ConcurrentLinkedQueue<Integer> newFds = new ConcurrentLinkedQueue<>();
    private final AtomicBoolean running = new AtomicBoolean(true);

    // Per-connection state: fd -> ConnectionState
    private final Map<Integer, ConnectionState> connections = new HashMap<>();

    // Fixed buffer free list
    private final int[] freeBuffers;
    private int freeCount;

    // Ring and arena (thread-confined)
    private IoUringRing ring;
    private Arena arena;
    private MemorySegment[] fixedBufferSegments;

    public WorkerThread(int workerId,
                        Map<Integer, MemorySegment> closeResponses,
                        Map<Integer, MemorySegment> keepAliveResponses) {
        super("worker-" + workerId);
        setDaemon(true);
        this.workerId = workerId;
        this.closeResponses = closeResponses;
        this.keepAliveResponses = keepAliveResponses;

        this.freeBuffers = new int[FIXED_BUF_COUNT];
        this.freeCount = FIXED_BUF_COUNT;
        for (int i = 0; i < FIXED_BUF_COUNT; i++) {
            freeBuffers[i] = i;
        }
    }

    /**
     * Called by acceptor thread to hand off a new client fd.
     */
    public void offerFd(int fd) {
        newFds.add(fd);
    }

    public void shutdown() {
        running.set(false);
    }

    @Override
    public void run() {
        try {
            arena = Arena.ofConfined();
            setupRingAndBuffers();
            eventLoop();
        } catch (Throwable t) {
            if (running.get()) {
                System.err.println("Worker " + workerId + " error: " + t.getMessage());
                t.printStackTrace();
            }
        } finally {
            if (ring != null) {
                try { ring.close(); } catch (Exception e) { /* ignore */ }
            }
            if (arena != null) {
                arena.close();
            }
        }
    }

    private void setupRingAndBuffers() throws Throwable {
        if (USE_SQPOLL) {
            ring = IoUringRing.createSqpoll(arena, RING_ENTRIES, SQPOLL_IDLE_MS);
        } else {
            ring = IoUringRing.create(arena, RING_ENTRIES);
        }
        System.out.println("Worker " + workerId + " ring created (fd=" + ring.fd()
                + ", sqpoll=" + ring.isSqpoll() + ")");

        // Allocate fixed buffers for recv
        fixedBufferSegments = new MemorySegment[FIXED_BUF_COUNT];
        for (int i = 0; i < FIXED_BUF_COUNT; i++) {
            fixedBufferSegments[i] = arena.allocate(FIXED_BUF_SIZE, 8);
        }

        // Register with kernel
        MemorySegment iovec = buildIovecArray(arena, fixedBufferSegments);
        ring.registerBuffers(iovec, FIXED_BUF_COUNT);
        System.out.println("Worker " + workerId + " registered " + FIXED_BUF_COUNT + " fixed buffers");
    }

    private void eventLoop() throws Throwable {
        while (running.get()) {
            // 1. Drain new fds from acceptor
            boolean drained = drainNewFds();

            // 2. Submit any pending SQEs (from drain or previous CQE processing)
            ring.submit();

            // 3. If no active connections and no new fds, brief sleep to avoid busy-spinning
            if (connections.isEmpty()) {
                if (!drained) {
                    Thread.sleep(1);
                }
                continue;
            }

            // 4. Non-blocking peek for completions (submit 0, minComplete 0, GETEVENTS)
            //    This returns immediately with whatever CQEs are available.
            //    If nothing is ready, we loop back and drain new fds again.
            ring.peekCompletions();

            // 5. Process CQEs (may be zero — that's fine)
            boolean hadCqe = false;
            hadCqe = ring.forEachCqe((userData, res, cqeFlags) -> {
                long opType = userData & ~UD_FD_MASK;
                int fd = (int) (userData & UD_FD_MASK);

                try {
                    if (opType == UD_RECV) {
                        handleRecv(fd, res);
                    } else if (opType == UD_SEND) {
                        handleSend(fd, res);
                    } else if (opType == UD_CLOSE) {
                        handleClose(fd);
                    }
                } catch (Throwable t) {
                    System.err.println("Worker " + workerId + " CQE error: " + t.getMessage());
                    closeConnection(fd);
                }
            });

            // 6. Submit any new SQEs generated during CQE processing
            ring.submit();

            // 7. If no CQEs and no new fds, do a short blocking wait to avoid 100% CPU spin
            if (!hadCqe && !drained) {
                ring.waitForCompletions(1);
            }
        }
    }

    private boolean drainNewFds() {
        boolean drained = false;
        Integer fd;
        while ((fd = newFds.poll()) != null) {
            drained = true;
            int bufIdx = allocBuffer();
            if (bufIdx < 0) {
                try { IoUringNative.close(fd); } catch (Throwable t) { /* ignore */ }
                continue;
            }
            connections.put(fd, new ConnectionState(bufIdx));
            submitRecv(fd, bufIdx);
        }
        return drained;
    }

    private void submitRecv(int fd, int bufIdx) {
        MemorySegment sqe = ring.getSqe();
        if (sqe == null) {
            closeConnection(fd);
            return;
        }
        sqeSetRecv(sqe, fd, fixedBufferSegments[bufIdx], FIXED_BUF_SIZE, UD_RECV | fd);
    }

    private void submitSend(int fd, MemorySegment responseBuf, int len) {
        MemorySegment sqe = ring.getSqe();
        if (sqe == null) {
            closeConnection(fd);
            return;
        }
        sqeSetSend(sqe, fd, responseBuf, len, UD_SEND | fd);
    }

    private void submitSendWithOffset(int fd, MemorySegment responseBuf, int offset, int len) {
        MemorySegment sqe = ring.getSqe();
        if (sqe == null) {
            closeConnection(fd);
            return;
        }
        MemorySegment slice = responseBuf.asSlice(offset, len);
        sqeSetSend(sqe, fd, slice, len, UD_SEND | fd);
    }

    private void submitClose(int fd) {
        MemorySegment sqe = ring.getSqe();
        if (sqe == null) {
            try { IoUringNative.close(fd); } catch (Throwable t) { /* ignore */ }
            freeConnection(fd);
            return;
        }
        sqeSetClose(sqe, fd, UD_CLOSE | fd);
    }

    private void handleRecv(int fd, int res) throws Throwable {
        if (res <= 0) {
            closeConnection(fd);
            return;
        }

        ConnectionState cs = connections.get(fd);
        if (cs == null) {
            submitClose(fd);
            return;
        }

        // Parse the HTTP request from the fixed buffer
        MemorySegment buf = fixedBufferSegments[cs.recvBufIdx];
        byte[] requestBytes = new byte[res];
        MemorySegment.copy(buf, ValueLayout.JAVA_BYTE, 0, requestBytes, 0, res);
        String request = new String(requestBytes, StandardCharsets.US_ASCII);

        int dataSize = parseSize(request);
        boolean keepAlive = parseKeepAlive(request);

        // Select the right pre-built response template
        Map<Integer, MemorySegment> responseMap = keepAlive ? keepAliveResponses : closeResponses;
        MemorySegment response = responseMap.get(dataSize);
        if (response == null) {
            response = responseMap.get(64);
        }

        cs.keepAlive = keepAlive;
        cs.sendResponse = response;
        cs.sendTotal = (int) response.byteSize();
        cs.sendOffset = 0;

        submitSend(fd, response, cs.sendTotal);
    }

    private void handleSend(int fd, int res) throws Throwable {
        ConnectionState cs = connections.get(fd);
        if (cs == null) {
            submitClose(fd);
            return;
        }

        if (res < 0) {
            closeConnection(fd);
            return;
        }

        cs.sendOffset += res;

        if (cs.sendOffset < cs.sendTotal) {
            // Partial send — send remaining
            int remaining = cs.sendTotal - cs.sendOffset;
            submitSendWithOffset(fd, cs.sendResponse, cs.sendOffset, remaining);
            return;
        }

        // Full response sent
        if (cs.keepAlive) {
            cs.sendResponse = null;
            cs.sendOffset = 0;
            cs.sendTotal = 0;
            submitRecv(fd, cs.recvBufIdx);
        } else {
            closeConnection(fd);
        }
    }

    private void handleClose(int fd) {
        freeConnection(fd);
    }

    private void closeConnection(int fd) {
        submitClose(fd);
    }

    private void freeConnection(int fd) {
        ConnectionState cs = connections.remove(fd);
        if (cs != null) {
            freeBuffer(cs.recvBufIdx);
        }
    }

    private int allocBuffer() {
        if (freeCount <= 0) return -1;
        return freeBuffers[--freeCount];
    }

    private void freeBuffer(int idx) {
        if (idx >= 0 && idx < FIXED_BUF_COUNT) {
            freeBuffers[freeCount++] = idx;
        }
    }

    private static int parseSize(String request) {
        try {
            int idx = request.indexOf("size=");
            if (idx == -1) return 64;
            int start = idx + 5;
            int end = start;
            while (end < request.length() && Character.isDigit(request.charAt(end))) {
                end++;
            }
            return Integer.parseInt(request.substring(start, end));
        } catch (Exception e) {
            return 64;
        }
    }

    private static boolean parseKeepAlive(String request) {
        String lower = request.toLowerCase();
        int idx = lower.indexOf("connection:");
        if (idx == -1) return false;
        int start = idx + 11;
        int end = lower.indexOf('\r', start);
        if (end == -1) end = lower.indexOf('\n', start);
        if (end == -1) end = lower.length();
        String value = lower.substring(start, end).trim();
        return value.contains("keep-alive");
    }

    private static class ConnectionState {
        final int recvBufIdx;
        boolean keepAlive;
        MemorySegment sendResponse;
        int sendTotal;
        int sendOffset;

        ConnectionState(int recvBufIdx) {
            this.recvBufIdx = recvBufIdx;
        }
    }
}
