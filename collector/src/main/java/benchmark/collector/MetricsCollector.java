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
    private volatile boolean running = true;

    // Collected data
    private final List<CpuSnapshot> cpuSnapshots = new ArrayList<>();
    private final List<ContextSwitchSnapshot> csSnapshots = new ArrayList<>();
    private final List<MemorySnapshot> memSnapshots = new ArrayList<>();
    private final List<FdSnapshot> fdSnapshots = new ArrayList<>();

    public MetricsCollector(long serverPid, long clientPid, int durationSeconds, Path outputDir, boolean noStrace) {
        this.serverPid = serverPid;
        this.clientPid = clientPid;
        this.durationSeconds = durationSeconds;
        this.outputDir = outputDir;
        this.noStrace = noStrace;
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("Usage: MetricsCollector <server_pid> <client_pid> <duration_sec> [output_dir]");
            System.exit(1);
        }
        long serverPid = Long.parseLong(args[0]);
        long clientPid = Long.parseLong(args[1]);
        int duration = Integer.parseInt(args[2]);
        String outDir = args.length > 3 ? args[3] : "results/default";
        boolean noStrace = args.length > 4 && "no-strace".equals(args[4]);

        Path outPath = Path.of(outDir);
        Files.createDirectories(outPath);

        MetricsCollector collector = new MetricsCollector(serverPid, clientPid, duration, outPath, noStrace);
        collector.run();
    }

    public void run() throws Exception {
        System.out.println("Collecting metrics: server_pid=" + serverPid +
                " client_pid=" + clientPid + " duration=" + durationSeconds + "s");

        // Start strace in background for syscall summary
        // (skip for FFM models — strace ptrace attachment crashes io_uring FFM servers)
        Process straceProcess = noStrace ? null : startStrace();

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
                // Send SIGINT to strace so it prints the syscall summary
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

    // ── CPU via pidstat ──

    private CpuSnapshot collectCpu(int sec) {
        double serverUser = 0, serverSys = 0;
        double clientUser = 0, clientSys = 0;
        try {
            double[] sv = pidstatCpu(serverPid);
            serverUser = sv[0];
            serverSys = sv[1];
        } catch (Exception ignored) {}
        try {
            double[] cl = pidstatCpu(clientPid);
            clientUser = cl[0];
            clientSys = cl[1];
        } catch (Exception ignored) {}
        return new CpuSnapshot(sec, serverUser, serverSys, serverUser + serverSys,
                clientUser, clientSys, clientUser + clientSys);
    }

    private double[] pidstatCpu(long pid) throws Exception {
        // pidstat -p <pid> 1 1 — one sample over 1 second
        // We use /proc/stat instead for instant snapshot to avoid blocking
        Path statPath = Path.of("/proc/" + pid + "/stat");
        if (!Files.exists(statPath)) return new double[]{0, 0};

        String stat = Files.readString(statPath).trim();
        // Fields after last ')': state, ppid, pgrp, session, tty_nr, tpgid, flags,
        //   minflt, cminflt, majflt, cmajflt, utime(13), stime(14), ...
        int closeParenIdx = stat.lastIndexOf(')');
        String[] fields = stat.substring(closeParenIdx + 2).split("\\s+");
        long utime = Long.parseLong(fields[11]); // index 13 in full, 11 after ')'
        long stime = Long.parseLong(fields[12]); // index 14 in full, 12 after ')'

        long ticksPerSec = 100; // standard on Linux (sysconf(_SC_CLK_TCK))
        int cpuCount = Runtime.getRuntime().availableProcessors();

        // Return as percentage of total CPU
        double userPct = (utime * 100.0) / (ticksPerSec * cpuCount);
        double sysPct = (stime * 100.0) / (ticksPerSec * cpuCount);
        return new double[]{userPct, sysPct};
    }

    // ── Context switches via /proc/pid/status ──

    private ContextSwitchSnapshot collectContextSwitches(int sec) {
        long serverVol = 0, serverInvol = 0;
        long clientVol = 0, clientInvol = 0;
        try {
            long[] sv = readContextSwitches(serverPid);
            serverVol = sv[0];
            serverInvol = sv[1];
        } catch (Exception ignored) {}
        try {
            long[] cl = readContextSwitches(clientPid);
            clientVol = cl[0];
            clientInvol = cl[1];
        } catch (Exception ignored) {}
        return new ContextSwitchSnapshot(sec, serverVol, serverInvol, clientVol, clientInvol);
    }

    private long[] readContextSwitches(long pid) throws IOException {
        Path statusPath = Path.of("/proc/" + pid + "/status");
        long voluntary = 0, involuntary = 0;
        for (String line : Files.readAllLines(statusPath)) {
            if (line.startsWith("voluntary_ctxt_switches:")) {
                voluntary = Long.parseLong(line.split(":\\s+")[1].trim());
            } else if (line.startsWith("nonvoluntary_ctxt_switches:")) {
                involuntary = Long.parseLong(line.split(":\\s+")[1].trim());
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
        // Format: "VmRSS:     12345 kB"
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
                    // Format: % time     seconds  usecs/call     calls    errors syscall
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
