package benchmark.client;

import org.HdrHistogram.Histogram;

import java.io.*;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class BenchmarkClient {

    private static final long HIGHEST_TRACKABLE_VALUE = 60_000_000L; // 60 seconds in microseconds

    private volatile boolean warmingUp = true;
    private volatile boolean running = true;

    private final String host;
    private final int port;
    private final int connections;
    private final int dataSize;
    private final int warmupSeconds;
    private final int durationSeconds;
    private final Path outputDir;

    // Shared metrics
    private final AtomicLong requestsCompleted = new AtomicLong();
    private final AtomicLong bytesSent = new AtomicLong();
    private final AtomicLong errors = new AtomicLong();
    private final Histogram latencyHistogram = new Histogram(HIGHEST_TRACKABLE_VALUE, 3);

    // Per-second snapshots
    private final ConcurrentLinkedQueue<ThroughputSnapshot> throughputSnapshots = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<LatencySnapshot> latencySnapshots = new ConcurrentLinkedQueue<>();

    public BenchmarkClient(String host, int port, int connections, int dataSize,
                           int warmupSeconds, int durationSeconds, Path outputDir) {
        this.host = host;
        this.port = port;
        this.connections = connections;
        this.dataSize = dataSize;
        this.warmupSeconds = warmupSeconds;
        this.durationSeconds = durationSeconds;
        this.outputDir = outputDir;
    }

    public static void main(String[] args) throws Exception {
        String host = getArg(args, 0, "localhost");
        int port = Integer.parseInt(getArg(args, 1, "8080"));
        int connections = Integer.parseInt(getArg(args, 2, "100"));
        int dataSize = Integer.parseInt(getArg(args, 3, "4096"));
        int warmupSeconds = Integer.parseInt(getArg(args, 4, "5"));
        int durationSeconds = Integer.parseInt(getArg(args, 5, "30"));
        String outputDir = getArg(args, 6, "results/default");

        Path outPath = Path.of(outputDir);
        Files.createDirectories(outPath);

        BenchmarkClient client = new BenchmarkClient(host, port, connections, dataSize,
                warmupSeconds, durationSeconds, outPath);
        client.run();
    }

    private static String getArg(String[] args, int index, String defaultValue) {
        return args.length > index ? args[index] : defaultValue;
    }

    public void run() throws Exception {
        System.out.println("Benchmark: host=" + host + " port=" + port +
                " connections=" + connections + " size=" + dataSize +
                " warmup=" + warmupSeconds + "s duration=" + durationSeconds + "s");
        System.out.println("Output: " + outputDir);

        byte[] requestBytes = buildRequest();

        // Launch worker virtual threads
        ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
        for (int i = 0; i < connections; i++) {
            executor.submit(() -> workerLoop(requestBytes));
        }

        // Warmup phase
        System.out.println("Warmup: " + warmupSeconds + " seconds...");
        Thread.sleep(warmupSeconds * 1000L);

        // Reset counters after warmup
        requestsCompleted.set(0);
        bytesSent.set(0);
        errors.set(0);
        synchronized (latencyHistogram) {
            latencyHistogram.reset();
        }
        warmingUp = false;

        // Measurement phase — collect per-second snapshots
        System.out.println("Measuring: " + durationSeconds + " seconds...");

        long prevRequests = 0;
        long prevBytes = 0;

        for (int sec = 1; sec <= durationSeconds; sec++) {
            Thread.sleep(1000L);

            long curRequests = requestsCompleted.get();
            long curBytes = bytesSent.get();
            long deltaRequests = curRequests - prevRequests;
            long deltaBytes = curBytes - prevBytes;
            double throughputRps = deltaRequests;
            double throughputMBs = deltaBytes / (1024.0 * 1024.0);  // MB/s
            double throughputMpps = deltaRequests / 1_000_000.0;     // Mpps (megapackets/s)

            throughputSnapshots.add(new ThroughputSnapshot(
                    sec, deltaRequests, deltaBytes, throughputRps, throughputMBs, throughputMpps));

            // Snapshot latency histogram
            Histogram intervalSnapshot;
            synchronized (latencyHistogram) {
                intervalSnapshot = latencyHistogram.copy();
                latencyHistogram.reset();
            }

            latencySnapshots.add(new LatencySnapshot(
                    sec,
                    intervalSnapshot.getValueAtPercentile(50),
                    intervalSnapshot.getValueAtPercentile(90),
                    intervalSnapshot.getValueAtPercentile(99),
                    intervalSnapshot.getValueAtPercentile(99.9),
                    intervalSnapshot.getMinValue(),
                    intervalSnapshot.getMaxValue(),
                    (long) intervalSnapshot.getMean(),
                    (long) intervalSnapshot.getStdDeviation()));

            System.out.printf("  [%2d] rps=%,d  p50=%,dus  p99=%,dus  errors=%d%n",
                    sec, deltaRequests,
                    intervalSnapshot.getValueAtPercentile(50),
                    intervalSnapshot.getValueAtPercentile(99),
                    errors.get());

            prevRequests = curRequests;
            prevBytes = curBytes;
        }

        // Stop workers
        running = false;
        executor.shutdownNow();
        executor.awaitTermination(5, TimeUnit.SECONDS);

        // Write CSV results
        writeThroughputCsv();
        writeLatencyCsv();

        System.out.println("Done. Total requests: " + requestsCompleted.get() +
                " errors: " + errors.get());
    }

    private byte[] buildRequest() {
        String request = "GET /data?size=" + dataSize + " HTTP/1.1\r\n" +
                "Host: " + host + "\r\n" +
                "Connection: close\r\n" +
                "\r\n";
        return request.getBytes(StandardCharsets.US_ASCII);
    }

    private void workerLoop(byte[] requestBytes) {
        byte[] readBuffer = new byte[65536];
        while (running) {
            long startNanos = System.nanoTime();
            try {
                try (Socket socket = new Socket()) {
                    socket.setTcpNoDelay(true);
                    socket.setSoTimeout(5000);
                    socket.connect(new InetSocketAddress(host, port), 3000);

                    OutputStream out = socket.getOutputStream();
                    out.write(requestBytes);
                    out.flush();

                    InputStream in = socket.getInputStream();
                    long totalRead = 0;
                    int n;
                    while ((n = in.read(readBuffer)) != -1) {
                        totalRead += n;
                    }

                    long elapsedUs = (System.nanoTime() - startNanos) / 1000;

                    if (!warmingUp) {
                        requestsCompleted.incrementAndGet();
                        bytesSent.addAndGet(totalRead);
                        synchronized (latencyHistogram) {
                            latencyHistogram.recordValue(Math.min(elapsedUs, HIGHEST_TRACKABLE_VALUE));
                        }
                    }
                }
            } catch (Exception e) {
                if (running) {
                    errors.incrementAndGet();
                }
            }
        }
    }

    private void writeThroughputCsv() throws IOException {
        Path file = outputDir.resolve("throughput.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,requests_completed,bytes_sent,throughput_rps,throughput_mbs,throughput_mpps\n");
        for (ThroughputSnapshot s : throughputSnapshots) {
            sb.append(s.sec).append(',')
              .append(s.requests).append(',')
              .append(s.bytes).append(',')
              .append(String.format("%.1f", s.rps)).append(',')
              .append(String.format("%.3f", s.mbs)).append(',')
              .append(String.format("%.6f", s.mpps)).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    private void writeLatencyCsv() throws IOException {
        Path file = outputDir.resolve("latency.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,p50_us,p90_us,p99_us,p999_us,min_us,max_us,mean_us,stddev_us\n");
        for (LatencySnapshot s : latencySnapshots) {
            sb.append(s.sec).append(',')
              .append(s.p50).append(',')
              .append(s.p90).append(',')
              .append(s.p99).append(',')
              .append(s.p999).append(',')
              .append(s.min).append(',')
              .append(s.max).append(',')
              .append(s.mean).append(',')
              .append(s.stddev).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    record ThroughputSnapshot(int sec, long requests, long bytes, double rps, double mbs, double mpps) {}
    record LatencySnapshot(int sec, long p50, long p90, long p99, long p999,
                           long min, long max, long mean, long stddev) {}
}
