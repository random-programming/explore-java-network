package benchmark.collector;

import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;

/**
 * Collects system-level metrics for server and client processes during benchmark.
 * Writes per-second CSV files: cpu.csv, context_switches.csv, memory.csv, fd_count.csv.
 * Runs strace separately to produce syscalls.csv (summary mode).
 */
public class MetricsCollector {

    private final long serverPid;
    private final long clientPid;
    private final int durationSeconds;
    private final Path outputDir;
    private final boolean noStrace;
    private final int serverCpuCount;
    private volatile boolean running = true;

    // Previous values for delta calculation
    private long prevServerUtime = -1, prevServerStime = -1;
    private long prevClientUtime = -1, prevClientStime = -1;
    private long prevServerVolCs = -1, prevServerInvolCs = -1;
    private long prevClientVolCs = -1, prevClientInvolCs = -1;

    // Collected data
    private final List<CpuSnapshot> cpuSnapshots = new ArrayList<>();
    private final List<ContextSwitchSnapshot> csSnapshots = new ArrayList<>();
    private final List<MemorySnapshot> memSnapshots = new ArrayList<>();
    private final List<FdSnapshot> fdSnapshots = new ArrayList<>();

    public MetricsCollector(long serverPid, long clientPid, int durationSeconds,
                            Path outputDir, boolean noStrace, int serverCpuCount) {
        this.serverPid = serverPid;
        this.clientPid = clientPid;
        this.durationSeconds = durationSeconds;
        this.outputDir = outputDir;
        this.noStrace = noStrace;
        this.serverCpuCount = serverCpuCount;
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 4) {
            System.err.println("Usage: MetricsCollector <server_pid> <client_pid> <duration_sec> <output_dir> [no-strace] [server_cpu_count]");
            System.exit(1);
        }
        long serverPid = Long.parseLong(args[0]);
        long clientPid = Long.parseLong(args[1]);
        int duration = Integer.parseInt(args[2]);
        String outDir = args[3];
        boolean noStrace = args.length > 4 && "no-strace".equals(args[4]);
        int serverCpuCount = args.length > 5 ? Integer.parseInt(args[5])
                : Runtime.getRuntime().availableProcessors();

        Path outPath = Path.of(outDir);
        Files.createDirectories(outPath);

        MetricsCollector collector = new MetricsCollector(
                serverPid, clientPid, duration, outPath, noStrace, serverCpuCount);
        collector.run();
    }

    public void run() throws Exception {
        System.out.println("Collecting metrics: server_pid=" + serverPid +
                " client_pid=" + clientPid + " duration=" + durationSeconds +
                "s cpu_count=" + serverCpuCount);

        // Start strace in background for syscall summary
        Process straceProcess = noStrace ? null : startStrace();

        // Take initial readings for delta calculation
        initDeltas();

        // Collect per-second metrics
        for (int sec = 1; sec <= durationSeconds && running; sec++) {
            long t0 = System.currentTimeMillis();

            cpuSnapshots.add(collectCpu(sec));
            csSnapshots.add(collectContextSwitches(sec));
            memSnapshots.add(collectMemory(sec));
            fdSnapshots.add(collectFdCount(sec));

            long elapsed = System.currentTimeMillis() - t0;
            long sleepMs = Math.max(0, 1000 - elapsed);
            if (sleepMs > 0) {
                Thread.sleep(sleepMs);
            }
        }

        // Stop strace gracefully (SIGINT makes strace print summary)
        if (straceProcess != null) {
            long stracePid = straceProcess.pid();
            try {
                new ProcessBuilder("kill", "-2", String.valueOf(stracePid))
                        .start().waitFor(2, TimeUnit.SECONDS);
            } catch (Exception ignored) {}
            straceProcess.waitFor(5, TimeUnit.SECONDS);
            if (straceProcess.isAlive()) {
                straceProcess.destroyForcibly();
                straceProcess.waitFor(2, TimeUnit.SECONDS);
            }
        }

        // Write CSV files
        writeCpuCsv();
        writeContextSwitchesCsv();
        writeMemoryCsv();
        writeFdCountCsv();
        parseSyscallsCsv();

        System.out.println("Metrics collection done. Output: " + outputDir);
    }

    public void stop() {
        running = false;
    }

    // ── Initialize previous values for delta calculation ──

    private void initDeltas() {
        try {
            long[] sv = readProcStatTimes(serverPid);
            prevServerUtime = sv[0];
            prevServerStime = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readProcStatTimes(clientPid);
            prevClientUtime = cl[0];
            prevClientStime = cl[1];
        } catch (Exception ignored) {}
        try {
            long[] sv = readContextSwitchesRaw(serverPid);
            prevServerVolCs = sv[0];
            prevServerInvolCs = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readContextSwitchesRaw(clientPid);
            prevClientVolCs = cl[0];
            prevClientInvolCs = cl[1];
        } catch (Exception ignored) {}
    }

    // ── CPU via /proc/pid/stat delta ──

    private CpuSnapshot collectCpu(int sec) {
        double serverUser = 0, serverSys = 0;
        double clientUser = 0, clientSys = 0;
        try {
            long[] sv = readProcStatTimes(serverPid);
            if (prevServerUtime >= 0) {
                long deltaU = sv[0] - prevServerUtime;
                long deltaS = sv[1] - prevServerStime;
                if (deltaU >= 0 && deltaS >= 0) {
                    serverUser = (deltaU * 100.0) / (100.0 * serverCpuCount);
                    serverSys = (deltaS * 100.0) / (100.0 * serverCpuCount);
                }
            }
            prevServerUtime = sv[0];
            prevServerStime = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readProcStatTimes(clientPid);
            if (prevClientUtime >= 0) {
                long deltaU = cl[0] - prevClientUtime;
                long deltaS = cl[1] - prevClientStime;
                if (deltaU >= 0 && deltaS >= 0) {
                    clientUser = (deltaU * 100.0) / (100.0 * serverCpuCount);
                    clientSys = (deltaS * 100.0) / (100.0 * serverCpuCount);
                }
            }
            prevClientUtime = cl[0];
            prevClientStime = cl[1];
        } catch (Exception ignored) {}
        return new CpuSnapshot(sec, serverUser, serverSys, serverUser + serverSys,
                clientUser, clientSys, clientUser + clientSys);
    }

    private long[] readProcStatTimes(long pid) throws Exception {
        // Sum CPU times across ALL threads in /proc/PID/task/*/stat
        // Reading only /proc/PID/stat returns aggregate for the process,
        // but /proc/PID/task/TID/stat gives per-thread times which we sum
        // for consistency with context switches collection.
        Path taskDir = Path.of("/proc/" + pid + "/task");
        long totalUtime = 0, totalStime = 0;
        if (Files.isDirectory(taskDir)) {
            try (var threads = Files.newDirectoryStream(taskDir)) {
                for (Path threadDir : threads) {
                    Path statPath = threadDir.resolve("stat");
                    try {
                        String stat = Files.readString(statPath).trim();
                        int closeParenIdx = stat.lastIndexOf(')');
                        String[] fields = stat.substring(closeParenIdx + 2).split("\\s+");
                        totalUtime += Long.parseLong(fields[11]);
                        totalStime += Long.parseLong(fields[12]);
                    } catch (IOException ignored) {
                        // Thread may have exited
                    }
                }
            }
        } else {
            // Fallback: read main process stat
            Path statPath = Path.of("/proc/" + pid + "/stat");
            if (!Files.exists(statPath)) return new long[]{0, 0};
            String stat = Files.readString(statPath).trim();
            int closeParenIdx = stat.lastIndexOf(')');
            String[] fields = stat.substring(closeParenIdx + 2).split("\\s+");
            totalUtime = Long.parseLong(fields[11]);
            totalStime = Long.parseLong(fields[12]);
        }
        return new long[]{totalUtime, totalStime};
    }

    // ── Context switches via /proc/pid/status (delta) ──

    private ContextSwitchSnapshot collectContextSwitches(int sec) {
        long serverVol = 0, serverInvol = 0;
        long clientVol = 0, clientInvol = 0;
        try {
            long[] sv = readContextSwitchesRaw(serverPid);
            if (prevServerVolCs >= 0) {
                serverVol = sv[0] - prevServerVolCs;
                serverInvol = sv[1] - prevServerInvolCs;
            }
            prevServerVolCs = sv[0];
            prevServerInvolCs = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readContextSwitchesRaw(clientPid);
            if (prevClientVolCs >= 0) {
                clientVol = cl[0] - prevClientVolCs;
                clientInvol = cl[1] - prevClientInvolCs;
            }
            prevClientVolCs = cl[0];
            prevClientInvolCs = cl[1];
        } catch (Exception ignored) {}
        return new ContextSwitchSnapshot(sec, serverVol, serverInvol, clientVol, clientInvol);
    }

    private long[] readContextSwitchesRaw(long pid) throws IOException {
        // Sum context switches across ALL threads in /proc/PID/task/*/status
        // Reading only /proc/PID/status returns CS for the main thread only,
        // missing 99%+ of switches from worker threads.
        Path taskDir = Path.of("/proc/" + pid + "/task");
        long voluntary = 0, involuntary = 0;
        if (Files.isDirectory(taskDir)) {
            try (var threads = Files.newDirectoryStream(taskDir)) {
                for (Path threadDir : threads) {
                    Path statusPath = threadDir.resolve("status");
                    try {
                        for (String line : Files.readAllLines(statusPath)) {
                            if (line.startsWith("voluntary_ctxt_switches:")) {
                                voluntary += Long.parseLong(line.split(":\\s+")[1].trim());
                            } else if (line.startsWith("nonvoluntary_ctxt_switches:")) {
                                involuntary += Long.parseLong(line.split(":\\s+")[1].trim());
                            }
                        }
                    } catch (IOException ignored) {
                        // Thread may have exited between listing and reading
                    }
                }
            }
        } else {
            // Fallback to main thread only
            Path statusPath = Path.of("/proc/" + pid + "/status");
            for (String line : Files.readAllLines(statusPath)) {
                if (line.startsWith("voluntary_ctxt_switches:")) {
                    voluntary = Long.parseLong(line.split(":\\s+")[1].trim());
                } else if (line.startsWith("nonvoluntary_ctxt_switches:")) {
                    involuntary = Long.parseLong(line.split(":\\s+")[1].trim());
                }
            }
        }
        return new long[]{voluntary, involuntary};
    }

    // ── Memory via /proc/pid/status ──

    private MemorySnapshot collectMemory(int sec) {
        long serverRss = 0, serverVsz = 0;
        long clientRss = 0;
        try {
            long[] sv = readMemory(serverPid);
            serverRss = sv[0];
            serverVsz = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readMemory(clientPid);
            clientRss = cl[0];
        } catch (Exception ignored) {}
        return new MemorySnapshot(sec, serverRss, serverVsz, 0, clientRss);
    }

    private long[] readMemory(long pid) throws IOException {
        Path statusPath = Path.of("/proc/" + pid + "/status");
        long rssKb = 0, vszKb = 0;
        for (String line : Files.readAllLines(statusPath)) {
            if (line.startsWith("VmRSS:")) {
                rssKb = parseProcKb(line);
            } else if (line.startsWith("VmSize:")) {
                vszKb = parseProcKb(line);
            }
        }
        return new long[]{rssKb, vszKb};
    }

    private long parseProcKb(String line) {
        String[] parts = line.split("\\s+");
        return Long.parseLong(parts[1]);
    }

    // ── File descriptors via /proc/pid/fd ──

    private FdSnapshot collectFdCount(int sec) {
        int serverFd = countFds(serverPid);
        int clientFd = countFds(clientPid);
        return new FdSnapshot(sec, serverFd, clientFd);
    }

    private int countFds(long pid) {
        try {
            File fdDir = new File("/proc/" + pid + "/fd");
            String[] list = fdDir.list();
            return list != null ? list.length : 0;
        } catch (Exception e) {
            return 0;
        }
    }

    // ── Strace for syscalls ──

    private Process startStrace() {
        try {
            ProcessBuilder pb = new ProcessBuilder(
                    "strace", "-f", "-c", "-S", "calls", "-p", String.valueOf(serverPid));
            pb.redirectErrorStream(true);
            pb.redirectOutput(outputDir.resolve("strace_raw.txt").toFile());
            return pb.start();
        } catch (Exception e) {
            System.err.println("Warning: could not start strace: " + e.getMessage());
            return null;
        }
    }

    private void parseSyscallsCsv() {
        Path rawFile = outputDir.resolve("strace_raw.txt");
        Path csvFile = outputDir.resolve("syscalls.csv");
        try {
            if (!Files.exists(rawFile)) {
                System.err.println("No strace output found, skipping syscalls.csv");
                return;
            }
            List<String> lines = Files.readAllLines(rawFile);
            StringBuilder sb = new StringBuilder();
            sb.append("syscall_name,count,errors,total_time_us,avg_time_us,pct_time\n");

            boolean inTable = false;
            for (String line : lines) {
                line = line.trim();
                if (line.startsWith("% time")) {
                    inTable = true;
                    continue;
                }
                if (line.startsWith("------") || line.isEmpty() || line.startsWith("100.00")) {
                    continue;
                }
                if (inTable && !line.isEmpty()) {
                    String[] parts = line.split("\\s+");
                    if (parts.length >= 6) {
                        String pctTime = parts[0];
                        String totalSec = parts[1];
                        String avgUsec = parts[2];
                        String calls = parts[3];
                        String errorsStr = parts.length >= 7 ? parts[4] : "0";
                        String syscall = parts[parts.length - 1];

                        long totalUs = (long) (Double.parseDouble(totalSec) * 1_000_000);
                        sb.append(syscall).append(',')
                          .append(calls).append(',')
                          .append(errorsStr).append(',')
                          .append(totalUs).append(',')
                          .append(avgUsec).append(',')
                          .append(pctTime).append('\n');
                    }
                }
            }
            Files.writeString(csvFile, sb.toString());
            System.out.println("Wrote " + csvFile);
        } catch (Exception e) {
            System.err.println("Warning: failed to parse strace output: " + e.getMessage());
        }
    }

    // ── CSV Writers ──

    private void writeCpuCsv() throws IOException {
        Path file = outputDir.resolve("cpu.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,server_user_pct,server_sys_pct,server_total_pct,client_user_pct,client_sys_pct,client_total_pct\n");
        for (CpuSnapshot s : cpuSnapshots) {
            sb.append(s.sec).append(',')
              .append(String.format("%.1f", s.serverUser)).append(',')
              .append(String.format("%.1f", s.serverSys)).append(',')
              .append(String.format("%.1f", s.serverTotal)).append(',')
              .append(String.format("%.1f", s.clientUser)).append(',')
              .append(String.format("%.1f", s.clientSys)).append(',')
              .append(String.format("%.1f", s.clientTotal)).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    private void writeContextSwitchesCsv() throws IOException {
        Path file = outputDir.resolve("context_switches.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,server_voluntary,server_involuntary,client_voluntary,client_involuntary\n");
        for (ContextSwitchSnapshot s : csSnapshots) {
            sb.append(s.sec).append(',')
              .append(s.serverVol).append(',')
              .append(s.serverInvol).append(',')
              .append(s.clientVol).append(',')
              .append(s.clientInvol).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    private void writeMemoryCsv() throws IOException {
        Path file = outputDir.resolve("memory.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,server_rss_kb,server_vsz_kb,server_heap_kb,client_rss_kb\n");
        for (MemorySnapshot s : memSnapshots) {
            sb.append(s.sec).append(',')
              .append(s.serverRss).append(',')
              .append(s.serverVsz).append(',')
              .append(s.serverHeap).append(',')
              .append(s.clientRss).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    private void writeFdCountCsv() throws IOException {
        Path file = outputDir.resolve("fd_count.csv");
        StringBuilder sb = new StringBuilder();
        sb.append("timestamp_sec,server_fd_count,client_fd_count\n");
        for (FdSnapshot s : fdSnapshots) {
            sb.append(s.sec).append(',')
              .append(s.serverFd).append(',')
              .append(s.clientFd).append('\n');
        }
        Files.writeString(file, sb.toString());
        System.out.println("Wrote " + file);
    }

    // ── Records ──

    record CpuSnapshot(int sec, double serverUser, double serverSys, double serverTotal,
                        double clientUser, double clientSys, double clientTotal) {}
    record ContextSwitchSnapshot(int sec, long serverVol, long serverInvol,
                                  long clientVol, long clientInvol) {}
    record MemorySnapshot(int sec, long serverRss, long serverVsz, long serverHeap, long clientRss) {}
    record FdSnapshot(int sec, int serverFd, int clientFd) {}
}
