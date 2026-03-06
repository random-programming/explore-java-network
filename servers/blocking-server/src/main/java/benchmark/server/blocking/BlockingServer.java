package benchmark.server.blocking;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public class BlockingServer {

    private static final int[] SUPPORTED_SIZES = {64, 512, 4096, 16384, 65536, 131072, 524288, 1048576};
    private static final Map<Integer, byte[]> DATA_BUFFERS = new HashMap<>();
    private static volatile boolean running = true;

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        int threads = args.length > 1 ? Integer.parseInt(args[1]) : Math.min(Runtime.getRuntime().availableProcessors() * 2, 2000);

        // Pre-generate data buffers
        Random random = new Random(42);
        for (int size : SUPPORTED_SIZES) {
            byte[] buf = new byte[size];
            random.nextBytes(buf);
            DATA_BUFFERS.put(size, buf);
        }

        ThreadPoolExecutor executor = new ThreadPoolExecutor(
                threads, threads, 60, TimeUnit.SECONDS,
                new LinkedBlockingQueue<>(50000),
                new ThreadPoolExecutor.CallerRunsPolicy()
        );

        ServerSocket serverSocket = new ServerSocket();
        serverSocket.setReuseAddress(true);
        serverSocket.bind(new InetSocketAddress("0.0.0.0", port));

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            running = false;
            executor.shutdownNow();
            try { serverSocket.close(); } catch (IOException ignored) {}
            System.out.println("Server stopped.");
        }));

        System.out.println("Server started on port " + port + " with " + threads + " threads (blocking)");

        while (running) {
            try {
                Socket clientSocket = serverSocket.accept();
                executor.submit(() -> handleConnection(clientSocket));
            } catch (IOException e) {
                if (running) {
                    System.err.println("Accept error: " + e.getMessage());
                }
            }
        }
    }

    private static void handleConnection(Socket socket) {
        try {
            socket.setTcpNoDelay(true);
            socket.setSoTimeout(5000);
            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();

            // Read HTTP request (simple parsing)
            StringBuilder requestBuilder = new StringBuilder(512);
            int b;
            int consecutiveNewlines = 0;
            while ((b = in.read()) != -1) {
                char c = (char) b;
                requestBuilder.append(c);
                if (c == '\n') {
                    consecutiveNewlines++;
                } else if (c != '\r') {
                    consecutiveNewlines = 0;
                }
                if (consecutiveNewlines >= 2) break;
                if (requestBuilder.length() > 4096) break; // prevent abuse
            }

            String request = requestBuilder.toString();
            int size = parseSize(request);
            byte[] data = DATA_BUFFERS.getOrDefault(size, DATA_BUFFERS.get(64));

            // Build HTTP response
            String header = "HTTP/1.1 200 OK\r\n" +
                    "Content-Length: " + data.length + "\r\n" +
                    "Content-Type: application/octet-stream\r\n" +
                    "Connection: close\r\n" +
                    "\r\n";
            out.write(header.getBytes(StandardCharsets.US_ASCII));
            out.write(data);
            out.flush();
        } catch (IOException e) {
            // Connection errors are expected under load
        } finally {
            try { socket.close(); } catch (IOException ignored) {}
        }
    }

    private static int parseSize(String request) {
        try {
            int idx = request.indexOf("size=");
            if (idx == -1) return 64;
            int end = idx + 5;
            int start = end;
            while (end < request.length() && Character.isDigit(request.charAt(end))) {
                end++;
            }
            return Integer.parseInt(request.substring(start, end));
        } catch (Exception e) {
            return 64;
        }
    }
}
