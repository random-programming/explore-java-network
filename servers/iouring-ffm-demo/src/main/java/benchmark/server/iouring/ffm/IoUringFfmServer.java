package benchmark.server.iouring.ffm;

import java.lang.foreign.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

import static benchmark.server.iouring.ffm.IoUringNative.*;

/**
 * Minimal HTTP server built entirely on io_uring via JDK 21 FFM (no JNI, no Netty).
 *
 * Demonstrates:
 *   - io_uring ring setup and mmap via FFM syscall()
 *   - SQE/CQE manipulation through MemorySegment
 *   - Accept -> Recv -> Send -> Close pipeline
 *   - Pre-allocated data buffers (same pattern as Netty servers)
 *
 * This is a single-threaded event loop for simplicity/clarity.
 */
public class IoUringFfmServer {

    private static final int RING_ENTRIES = 256;
    private static final int RECV_BUF_SIZE = 2048;

    // user_data encoding: high 8 bits = operation type, low 56 bits = fd or context
    private static final long UD_ACCEPT = 1L << 56;
    private static final long UD_RECV   = 2L << 56;
    private static final long UD_SEND   = 3L << 56;
    private static final long UD_CLOSE  = 4L << 56;
    private static final long UD_FD_MASK = (1L << 56) - 1;

    private static final int[] SUPPORTED_SIZES = {64, 512, 4096, 16384, 65536, 131072, 524288, 1048576};
    private static final Map<Integer, byte[]> DATA_BUFFERS = new HashMap<>();

    // Ring state
    private int ringFd;
    private MemorySegment sqRing;
    private MemorySegment cqRing;
    private MemorySegment sqes;

    // Offsets from params
    private int sqMask;
    private int cqMask;
    private long sqHeadOff, sqTailOff, sqArrayOff;
    private long cqHeadOff, cqTailOff, cqCqesOff;

    private final Arena arena = Arena.ofConfined();

    // Per-connection receive buffers: fd -> MemorySegment
    private final Map<Integer, MemorySegment> recvBuffers = new HashMap<>();
    // Per-connection send buffers
    private final Map<Integer, MemorySegment> sendBuffers = new HashMap<>();

    public static void main(String[] args) throws Throwable {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;

        // Pre-generate data buffers
        Random random = new Random(42);
        for (int size : SUPPORTED_SIZES) {
            byte[] buf = new byte[size];
            random.nextBytes(buf);
            DATA_BUFFERS.put(size, buf);
        }

        IoUringFfmServer server = new IoUringFfmServer();
        server.start(port);
    }

    public void start(int port) throws Throwable {
        // 1. Create listening socket
        int listenFd = createListeningSocket(port);
        System.out.println("Server started on port " + port + " (io_uring FFM, single-threaded)");

        // 2. Setup io_uring ring
        setupRing();
        System.out.println("io_uring ring initialized: entries=" + RING_ENTRIES + " fd=" + ringFd);

        // 3. Submit initial accept
        submitAccept(listenFd);
        flush();

        // 4. Event loop
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Server stopped.");
        }));

        eventLoop(listenFd);
    }

    private int createListeningSocket(int port) throws Throwable {
        int fd = socket(AF_INET, SOCK_STREAM, 0);

        // SO_REUSEADDR
        MemorySegment optval = arena.allocate(ValueLayout.JAVA_INT, 1);
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, optval, 4);

        // SO_REUSEPORT
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, optval, 4);

        // Bind
        MemorySegment addr = buildSockaddrIn(arena, port);
        bind(fd, addr, 16);

        // Listen
        listen(fd, 65535);

        return fd;
    }

    private void setupRing() throws Throwable {
        // Allocate io_uring_params
        MemorySegment params = arena.allocate(PARAMS_SIZE);
        params.fill((byte) 0);

        // Setup ring
        ringFd = ioUringSetup(RING_ENTRIES, params);

        // Read back actual sizes and offsets
        int sqEntries = params.get(ValueLayout.JAVA_INT, PARAMS_SQ_ENTRIES_OFF);
        int cqEntries = params.get(ValueLayout.JAVA_INT, PARAMS_CQ_ENTRIES_OFF);

        // Read sq_off
        sqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_HEAD));
        sqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_TAIL));
        long sqRingMaskOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_RING_MASK));
        sqArrayOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_ARRAY));

        // Read cq_off
        cqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_HEAD));
        cqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_TAIL));
        long cqRingMaskOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_RING_MASK));
        cqCqesOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_CQES));

        // mmap SQ ring
        long sqRingSize = sqArrayOff + sqEntries * 4L;
        sqRing = mmap(sqRingSize, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_SQ_RING);

        // mmap CQ ring
        long cqRingSize = cqCqesOff + cqEntries * CQE_SIZE;
        cqRing = mmap(cqRingSize, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_CQ_RING);

        // mmap SQEs
        sqes = mmap(sqEntries * SQE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_SQES);

        // Read masks
        sqMask = sqRing.get(ValueLayout.JAVA_INT, sqRingMaskOff);
        cqMask = cqRing.get(ValueLayout.JAVA_INT, cqRingMaskOff);
    }

    private MemorySegment getSqe() {
        int tail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
        int head = sqRing.get(ValueLayout.JAVA_INT, sqHeadOff);

        if (tail - head >= RING_ENTRIES) {
            return null; // SQ is full
        }

        int index = tail & sqMask;

        // Update SQ array
        sqRing.set(ValueLayout.JAVA_INT, sqArrayOff + index * 4L, index);

        // Advance tail
        sqRing.set(ValueLayout.JAVA_INT, sqTailOff, tail + 1);

        // Return SQE at this index
        return sqes.asSlice(index * SQE_SIZE, SQE_SIZE);
    }

    private void flush() throws Throwable {
        int tail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
        int head = sqRing.get(ValueLayout.JAVA_INT, sqHeadOff);
        int toSubmit = tail - head;
        if (toSubmit > 0) {
            ioUringEnter(ringFd, toSubmit, 0, 0);
        }
    }

    private void submitAccept(int listenFd) {
        MemorySegment sqe = getSqe();
        if (sqe == null) return;

        MemorySegment clientAddr = arena.allocate(16); // sockaddr_in
        MemorySegment addrLen = arena.allocate(ValueLayout.JAVA_INT, 16);

        sqeSetAccept(sqe, listenFd, clientAddr, addrLen, 0, UD_ACCEPT | listenFd);
    }

    private void submitRecv(int clientFd) {
        MemorySegment sqe = getSqe();
        if (sqe == null) return;

        MemorySegment buf = recvBuffers.computeIfAbsent(clientFd,
                k -> arena.allocate(RECV_BUF_SIZE));

        sqeSetRecv(sqe, clientFd, buf, RECV_BUF_SIZE, UD_RECV | clientFd);
    }

    private void submitSend(int clientFd, byte[] responseBytes) {
        MemorySegment sqe = getSqe();
        if (sqe == null) return;

        MemorySegment sendBuf = arena.allocate(responseBytes.length);
        sendBuf.copyFrom(MemorySegment.ofArray(responseBytes));
        sendBuffers.put(clientFd, sendBuf);

        sqeSetSend(sqe, clientFd, sendBuf, responseBytes.length, UD_SEND | clientFd);
    }

    private void submitClose(int clientFd) {
        MemorySegment sqe = getSqe();
        if (sqe == null) return;

        sqeSetClose(sqe, clientFd, UD_CLOSE | clientFd);
    }

    private void eventLoop(int listenFd) throws Throwable {
        while (true) {
            // Wait for at least 1 completion
            ioUringEnter(ringFd, 0, 1, IORING_ENTER_GETEVENTS);

            // Process completions
            int cqHead = cqRing.get(ValueLayout.JAVA_INT, cqHeadOff);
            int cqTail = cqRing.get(ValueLayout.JAVA_INT, cqTailOff);

            boolean submitted = false;

            while (cqHead != cqTail) {
                int index = cqHead & cqMask;
                MemorySegment cqe = cqRing.asSlice(cqCqesOff + index * CQE_SIZE, CQE_SIZE);

                long userData = cqe.get(ValueLayout.JAVA_LONG, CQE_USER_DATA_OFF);
                int res = cqe.get(ValueLayout.JAVA_INT, CQE_RES_OFF);

                long opType = userData & ~UD_FD_MASK;
                int fd = (int) (userData & UD_FD_MASK);

                if (opType == UD_ACCEPT) {
                    handleAccept(listenFd, res);
                    submitted = true;
                } else if (opType == UD_RECV) {
                    handleRecv(fd, res);
                    submitted = true;
                } else if (opType == UD_SEND) {
                    handleSend(fd, res);
                    submitted = true;
                } else if (opType == UD_CLOSE) {
                    handleClose(fd);
                }

                cqHead++;
            }

            // Advance CQ head
            cqRing.set(ValueLayout.JAVA_INT, cqHeadOff, cqHead);

            // Flush any new submissions
            if (submitted) {
                flush();
            }
        }
    }

    private void handleAccept(int listenFd, int res) {
        if (res >= 0) {
            int clientFd = res;
            // Submit recv for this client
            submitRecv(clientFd);
        }
        // Always re-arm accept
        submitAccept(listenFd);
    }

    private void handleRecv(int clientFd, int res) {
        if (res <= 0) {
            // Connection closed or error — close fd
            submitClose(clientFd);
            recvBuffers.remove(clientFd);
            return;
        }

        // Parse HTTP request to extract size
        MemorySegment buf = recvBuffers.get(clientFd);
        if (buf == null) {
            submitClose(clientFd);
            return;
        }

        byte[] requestBytes = new byte[res];
        MemorySegment.copy(buf, ValueLayout.JAVA_BYTE, 0, requestBytes, 0, res);
        String request = new String(requestBytes, StandardCharsets.US_ASCII);

        int dataSize = parseSize(request);
        byte[] dataBody = DATA_BUFFERS.getOrDefault(dataSize, DATA_BUFFERS.get(64));

        // Build HTTP response
        byte[] response = buildResponse(dataBody);

        // Submit send
        submitSend(clientFd, response);

        // Free recv buffer for this fd
        recvBuffers.remove(clientFd);
    }

    private void handleSend(int clientFd, int res) {
        sendBuffers.remove(clientFd);
        // Close connection after send (Connection: close)
        submitClose(clientFd);
    }

    private void handleClose(int clientFd) {
        recvBuffers.remove(clientFd);
        sendBuffers.remove(clientFd);
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

    private static byte[] buildResponse(byte[] body) {
        String header = "HTTP/1.1 200 OK\r\n" +
                "Content-Length: " + body.length + "\r\n" +
                "Content-Type: application/octet-stream\r\n" +
                "Connection: close\r\n" +
                "\r\n";
        byte[] headerBytes = header.getBytes(StandardCharsets.US_ASCII);
        byte[] response = new byte[headerBytes.length + body.length];
        System.arraycopy(headerBytes, 0, response, 0, headerBytes.length);
        System.arraycopy(body, 0, response, headerBytes.length, body.length);
        return response;
    }
}
