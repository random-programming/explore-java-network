package benchmark.server.iouring.ffm;

import java.lang.foreign.*;
import java.lang.invoke.MethodHandle;

/**
 * Micro-benchmark comparing JNI vs FFM call overhead.
 *
 * Measures the cost of crossing the Java->native boundary by calling
 * a trivial native function (getpid) many times through both mechanisms.
 *
 * FFM path: Linker.nativeLinker().downcallHandle(getpid, ...)
 * JNI path: ProcessHandle.current().pid() (which internally uses JNI)
 *
 * Also benchmarks io_uring NOP submissions to measure the overhead of
 * the full FFM syscall path for io_uring operations.
 */
public class JniVsFfmBenchmark {

    private static final int WARMUP_ITERATIONS = 100_000;
    private static final int BENCH_ITERATIONS  = 10_000_000;
    private static final int URING_NOP_ITERATIONS = 1_000_000;

    public static void main(String[] args) throws Throwable {
        System.out.println("=== JNI vs FFM Overhead Benchmark ===");
        System.out.println("Warmup: " + WARMUP_ITERATIONS + " iterations");
        System.out.println("Benchmark: " + BENCH_ITERATIONS + " iterations");
        System.out.println();

        benchmarkGetpid();
        System.out.println();
        benchmarkIoUringNop();
    }

    // ── getpid() benchmark ──

    private static void benchmarkGetpid() throws Throwable {
        // Setup FFM handle for getpid()
        Linker linker = Linker.nativeLinker();
        SymbolLookup lookup = SymbolLookup.loaderLookup().or(linker.defaultLookup());
        MethodHandle getpidFfm = linker.downcallHandle(
                lookup.find("getpid").orElseThrow(),
                FunctionDescriptor.of(ValueLayout.JAVA_INT));

        // === FFM: getpid() ===
        System.out.println("--- getpid() via FFM ---");

        // Warmup
        for (int i = 0; i < WARMUP_ITERATIONS; i++) {
            int pid = (int) getpidFfm.invokeExact();
        }

        // Bench
        long startFfm = System.nanoTime();
        for (int i = 0; i < BENCH_ITERATIONS; i++) {
            int pid = (int) getpidFfm.invokeExact();
        }
        long elapsedFfm = System.nanoTime() - startFfm;
        double nsPerCallFfm = (double) elapsedFfm / BENCH_ITERATIONS;
        System.out.printf("  Total: %,d ns  |  Per call: %.1f ns%n", elapsedFfm, nsPerCallFfm);

        // === JNI: ProcessHandle.current().pid() ===
        System.out.println("--- getpid() via JNI (ProcessHandle) ---");

        // Warmup
        for (int i = 0; i < WARMUP_ITERATIONS; i++) {
            long pid = ProcessHandle.current().pid();
        }

        // Bench
        long startJni = System.nanoTime();
        for (int i = 0; i < BENCH_ITERATIONS; i++) {
            long pid = ProcessHandle.current().pid();
        }
        long elapsedJni = System.nanoTime() - startJni;
        double nsPerCallJni = (double) elapsedJni / BENCH_ITERATIONS;
        System.out.printf("  Total: %,d ns  |  Per call: %.1f ns%n", elapsedJni, nsPerCallJni);

        // === Comparison ===
        System.out.println("--- Comparison ---");
        System.out.printf("  FFM:  %.1f ns/call%n", nsPerCallFfm);
        System.out.printf("  JNI:  %.1f ns/call%n", nsPerCallJni);
        double ratio = nsPerCallJni / nsPerCallFfm;
        if (ratio > 1.0) {
            System.out.printf("  FFM is %.2fx faster than JNI%n", ratio);
        } else {
            System.out.printf("  JNI is %.2fx faster than FFM%n", 1.0 / ratio);
        }
    }

    // ── io_uring NOP benchmark ──

    private static void benchmarkIoUringNop() throws Throwable {
        System.out.println("--- io_uring NOP via FFM (syscall overhead) ---");

        try (Arena arena = Arena.ofConfined()) {
            // Setup a minimal io_uring ring
            MemorySegment params = arena.allocate(IoUringNative.PARAMS_SIZE);
            params.fill((byte) 0);

            int ringFd = IoUringNative.ioUringSetup(32, params);

            int sqEntries = params.get(ValueLayout.JAVA_INT, IoUringNative.PARAMS_SQ_ENTRIES_OFF);

            // Read offsets
            long sqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.SQ_OFF_HEAD));
            long sqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.SQ_OFF_TAIL));
            long sqRingMaskOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.SQ_OFF_RING_MASK));
            long sqArrayOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.SQ_OFF_ARRAY));

            long cqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.CQ_OFF_HEAD));
            long cqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.CQ_OFF_TAIL));

            // mmap rings
            long sqRingSize = sqArrayOff + sqEntries * 4L;
            MemorySegment sqRing = IoUringNative.mmap(sqRingSize,
                    IoUringNative.PROT_READ | IoUringNative.PROT_WRITE,
                    IoUringNative.MAP_SHARED | IoUringNative.MAP_POPULATE,
                    ringFd, IoUringNative.IORING_OFF_SQ_RING);

            int cqEntries = params.get(ValueLayout.JAVA_INT, IoUringNative.PARAMS_CQ_ENTRIES_OFF);
            long cqCqesOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, IoUringNative.CQ_OFF_CQES));
            long cqRingSize = cqCqesOff + cqEntries * IoUringNative.CQE_SIZE;
            MemorySegment cqRing = IoUringNative.mmap(cqRingSize,
                    IoUringNative.PROT_READ | IoUringNative.PROT_WRITE,
                    IoUringNative.MAP_SHARED | IoUringNative.MAP_POPULATE,
                    ringFd, IoUringNative.IORING_OFF_CQ_RING);

            MemorySegment sqes = IoUringNative.mmap(sqEntries * IoUringNative.SQE_SIZE,
                    IoUringNative.PROT_READ | IoUringNative.PROT_WRITE,
                    IoUringNative.MAP_SHARED | IoUringNative.MAP_POPULATE,
                    ringFd, IoUringNative.IORING_OFF_SQES);

            int sqMask = sqRing.get(ValueLayout.JAVA_INT, sqRingMaskOff);

            // Warmup: submit + complete NOP operations
            for (int i = 0; i < WARMUP_ITERATIONS && i < 10_000; i++) {
                submitNop(sqRing, sqes, sqTailOff, sqHeadOff, sqArrayOff, sqMask, i);
                IoUringNative.ioUringEnter(ringFd, 1, 1, IoUringNative.IORING_ENTER_GETEVENTS);
                advanceCqHead(cqRing, cqHeadOff, cqTailOff);
            }

            // Benchmark: submit + complete NOP one at a time
            long startNop = System.nanoTime();
            for (int i = 0; i < URING_NOP_ITERATIONS; i++) {
                submitNop(sqRing, sqes, sqTailOff, sqHeadOff, sqArrayOff, sqMask, i);
                IoUringNative.ioUringEnter(ringFd, 1, 1, IoUringNative.IORING_ENTER_GETEVENTS);
                advanceCqHead(cqRing, cqHeadOff, cqTailOff);
            }
            long elapsedNop = System.nanoTime() - startNop;
            double nsPerNop = (double) elapsedNop / URING_NOP_ITERATIONS;

            System.out.printf("  Iterations: %,d%n", URING_NOP_ITERATIONS);
            System.out.printf("  Total: %,d ns  |  Per NOP round-trip: %.1f ns (%.1f us)%n",
                    elapsedNop, nsPerNop, nsPerNop / 1000.0);
            System.out.printf("  This includes: submit SQE + io_uring_enter(submit=1, wait=1) + consume CQE%n");

            IoUringNative.close(ringFd);
        }
    }

    private static void submitNop(MemorySegment sqRing, MemorySegment sqes,
                                   long sqTailOff, long sqHeadOff, long sqArrayOff,
                                   int sqMask, long userData) {
        int tail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
        int index = tail & sqMask;
        sqRing.set(ValueLayout.JAVA_INT, sqArrayOff + index * 4L, index);

        MemorySegment sqe = sqes.asSlice(index * IoUringNative.SQE_SIZE, IoUringNative.SQE_SIZE);
        IoUringNative.sqeSetOp(sqe, (byte) IoUringNative.IORING_OP_NOP, -1, 0L, 0, userData);

        sqRing.set(ValueLayout.JAVA_INT, sqTailOff, tail + 1);
    }

    private static void advanceCqHead(MemorySegment cqRing, long cqHeadOff, long cqTailOff) {
        int head = cqRing.get(ValueLayout.JAVA_INT, cqHeadOff);
        int tail = cqRing.get(ValueLayout.JAVA_INT, cqTailOff);
        if (head != tail) {
            cqRing.set(ValueLayout.JAVA_INT, cqHeadOff, head + 1);
        }
    }
}
