package benchmark.server.iouring.ffm.mt;

import java.lang.foreign.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

import static benchmark.server.iouring.ffm.mt.IoUringNative.*;

/**
 * Multi-threaded HTTP server using io_uring via JDK 21 FFM.
 *
 * Architecture:
 *   - Acceptor thread (main): own ring, single-shot accept with re-arm, distributes fds round-robin
 *   - N worker threads: each owns a ring + fixed buffers, handles recv/send/close
 *   - Pre-built response templates in Arena.global() (stable addresses, zero per-request alloc)
 *   - Supports both Connection: close and Connection: keep-alive
 */
public class IoUringFfmMtServer {

    private static final int ACCEPTOR_RING_ENTRIES = 256;
    private static final int[] SUPPORTED_SIZES = {64, 512, 4096, 16384, 65536, 131072, 524288, 1048576};

    // user_data encoding for acceptor
    private static final long UD_ACCEPT = 1L << 56;
    private static final long UD_FD_MASK = (1L << 56) - 1;

    public static void main(String[] args) throws Throwable {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        int numWorkers = args.length > 1 ? Integer.parseInt(args[1])
                : Math.max(1, Runtime.getRuntime().availableProcessors() / 2);

        System.out.println("io_uring FFM Multi-Threaded Server");
        System.out.println("  Port: " + port);
        System.out.println("  Workers: " + numWorkers);
        System.out.println("  CPUs: " + Runtime.getRuntime().availableProcessors());

        // 1. Pre-generate data buffers (same random seed as all other servers)
        Random random = new Random(42);
        Map<Integer, byte[]> dataBuffers = new HashMap<>();
        for (int size : SUPPORTED_SIZES) {
            byte[] buf = new byte[size];
            random.nextBytes(buf);
            dataBuffers.put(size, buf);
        }

        // 2. Pre-build response templates in Arena.global() — stable addresses, shared read-only
        Map<Integer, MemorySegment> closeResponses = new HashMap<>();
        Map<Integer, MemorySegment> keepAliveResponses = new HashMap<>();
        for (int size : SUPPORTED_SIZES) {
            byte[] body = dataBuffers.get(size);
            closeResponses.put(size, buildResponseTemplate(body, false));
            keepAliveResponses.put(size, buildResponseTemplate(body, true));
        }

        // 3. Create listening socket
        Arena setupArena = Arena.ofConfined();
        int listenFd = createListeningSocket(setupArena, port);
        System.out.println("Listening socket fd=" + listenFd);

        // 4. Start worker threads
        WorkerThread[] workers = new WorkerThread[numWorkers];
        for (int i = 0; i < numWorkers; i++) {
            workers[i] = new WorkerThread(i, closeResponses, keepAliveResponses);
            workers[i].start();
        }

        // Give workers time to initialize their rings
        Thread.sleep(500);

        // 5. Shutdown hook
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down...");
            for (WorkerThread w : workers) {
                w.shutdown();
            }
            try {
                for (WorkerThread w : workers) {
                    w.join(2000);
                }
                IoUringNative.close(listenFd);
            } catch (Throwable t) {
                // ignore during shutdown
            }
            setupArena.close();
            System.out.println("Server stopped.");
        }));

        System.out.println("Server started on port " + port + " (io_uring FFM, multi-threaded)");

        // 6. Acceptor loop
        acceptorLoop(setupArena, listenFd, workers);
    }

    private static int createListeningSocket(Arena arena, int port) throws Throwable {
        int fd = socket(AF_INET, SOCK_STREAM, 0);

        MemorySegment optval = arena.allocate(ValueLayout.JAVA_INT, 1);
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, optval, 4);
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, optval, 4);

        MemorySegment addr = buildSockaddrIn(arena, port);
        bind(fd, addr, 16);
        listen(fd, 65535);

        return fd;
    }

    private static void acceptorLoop(Arena arena, int listenFd, WorkerThread[] workers) throws Throwable {
        IoUringRing ring = IoUringRing.create(arena, ACCEPTOR_RING_ENTRIES);
        System.out.println("Acceptor ring created (fd=" + ring.fd() + ")");

        MemorySegment clientAddr = arena.allocate(16);
        MemorySegment addrLen = arena.allocate(ValueLayout.JAVA_INT, 16);

        // Submit initial accept
        submitAccept(ring, listenFd, clientAddr, addrLen);
        ring.submit();

        int workerIdx = 0;
        final int numWorkers = workers.length;
        boolean needRearm = false;

        while (true) {
            // Wait for at least 1 completion
            ring.waitForCompletions(1);

            // Process CQEs
            final int[] widx = {workerIdx};
            final boolean[] rearm = {false};
            ring.forEachCqe((userData, res, cqeFlags) -> {
                long opType = userData & ~UD_FD_MASK;
                if (opType != UD_ACCEPT) return;

                if (res >= 0) {
                    workers[widx[0] % numWorkers].offerFd(res);
                    widx[0]++;
                }
                rearm[0] = true;
            });
            workerIdx = widx[0];

            // Re-arm accept ONCE after processing all CQEs (not inside the loop)
            if (rearm[0]) {
                submitAccept(ring, listenFd, clientAddr, addrLen);
            }

            // Submit re-armed accept
            ring.submit();
        }
    }

    private static void submitAccept(IoUringRing ring, int listenFd,
                                      MemorySegment clientAddr, MemorySegment addrLen) {
        MemorySegment sqe = ring.getSqe();
        if (sqe == null) {
            System.err.println("WARN: acceptor SQ full, accept lost!");
            return;
        }
        sqeSetAccept(sqe, listenFd, clientAddr, addrLen, 0, UD_ACCEPT | listenFd);
    }

    private static MemorySegment buildResponseTemplate(byte[] body, boolean keepAlive) {
        String connHeader = keepAlive ? "Connection: keep-alive" : "Connection: close";
        String header = "HTTP/1.1 200 OK\r\n" +
                "Content-Length: " + body.length + "\r\n" +
                "Content-Type: application/octet-stream\r\n" +
                connHeader + "\r\n" +
                "\r\n";
        byte[] headerBytes = header.getBytes(StandardCharsets.US_ASCII);
        byte[] full = new byte[headerBytes.length + body.length];
        System.arraycopy(headerBytes, 0, full, 0, headerBytes.length);
        System.arraycopy(body, 0, full, headerBytes.length, body.length);

        MemorySegment seg = Arena.global().allocate(full.length, 8);
        seg.copyFrom(MemorySegment.ofArray(full));
        return seg;
    }
}
