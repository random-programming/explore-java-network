package benchmark.server.iouring.ffm.mt;

import java.lang.foreign.*;
import java.lang.invoke.VarHandle;

import static benchmark.server.iouring.ffm.mt.IoUringNative.*;

/**
 * Encapsulates one io_uring ring instance with fixed buffer registration.
 * CQ size = 2x SQ to prevent overflow under burst load.
 * Supports optional SQPOLL mode for kernel-side SQ polling.
 */
public class IoUringRing implements AutoCloseable {

    private final int ringFd;
    private final int entries;

    private final MemorySegment sqRing;
    private final MemorySegment cqRing;
    private final MemorySegment sqes;

    private final int sqMask;
    private final int cqMask;
    private final long sqHeadOff, sqTailOff, sqArrayOff;
    private final long cqHeadOff, cqTailOff, cqCqesOff;

    // SQPOLL support
    private final boolean sqpollEnabled;
    private final long sqFlagsOff;
    private int localTail; // batched tail for SQPOLL mode

    private boolean buffersRegistered = false;

    private IoUringRing(int ringFd, int entries,
                        MemorySegment sqRing, MemorySegment cqRing, MemorySegment sqes,
                        int sqMask, int cqMask,
                        long sqHeadOff, long sqTailOff, long sqArrayOff,
                        long cqHeadOff, long cqTailOff, long cqCqesOff,
                        boolean sqpollEnabled, long sqFlagsOff) {
        this.ringFd = ringFd;
        this.entries = entries;
        this.sqRing = sqRing;
        this.cqRing = cqRing;
        this.sqes = sqes;
        this.sqMask = sqMask;
        this.cqMask = cqMask;
        this.sqHeadOff = sqHeadOff;
        this.sqTailOff = sqTailOff;
        this.sqArrayOff = sqArrayOff;
        this.cqHeadOff = cqHeadOff;
        this.cqTailOff = cqTailOff;
        this.cqCqesOff = cqCqesOff;
        this.sqpollEnabled = sqpollEnabled;
        this.sqFlagsOff = sqFlagsOff;
        this.localTail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
    }

    /**
     * Create a standard io_uring ring (no SQPOLL).
     */
    public static IoUringRing create(Arena arena, int entries) throws Throwable {
        return createInternal(arena, entries, false, 0);
    }

    /**
     * Create an io_uring ring with SQPOLL enabled.
     * The kernel spawns a polling thread that picks up SQEs without needing io_uring_enter.
     *
     * @param arena     arena for params allocation
     * @param entries   SQ entries
     * @param sqIdleMs  SQPOLL thread idle timeout in ms before sleeping
     */
    public static IoUringRing createSqpoll(Arena arena, int entries, int sqIdleMs) throws Throwable {
        return createInternal(arena, entries, true, sqIdleMs);
    }

    private static IoUringRing createInternal(Arena arena, int entries,
                                               boolean sqpoll, int sqIdleMs) throws Throwable {
        MemorySegment params = arena.allocate(PARAMS_SIZE);
        params.fill((byte) 0);

        // CQ size = 2x SQ to prevent overflow under burst load
        int flags = IORING_SETUP_CQSIZE;
        if (sqpoll) {
            flags |= IORING_SETUP_SQPOLL;
        }
        params.set(ValueLayout.JAVA_INT, PARAMS_FLAGS_OFF, flags);
        params.set(ValueLayout.JAVA_INT, PARAMS_CQ_ENTRIES_OFF, entries * 2);
        if (sqpoll) {
            params.set(ValueLayout.JAVA_INT, PARAMS_SQ_THREAD_IDLE_OFF, sqIdleMs);
        }

        int ringFd = ioUringSetup(entries, params);

        int sqEntries = params.get(ValueLayout.JAVA_INT, PARAMS_SQ_ENTRIES_OFF);
        int cqEntries = params.get(ValueLayout.JAVA_INT, PARAMS_CQ_ENTRIES_OFF);

        long sqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_HEAD));
        long sqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_TAIL));
        long sqRingMaskOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_RING_MASK));
        long sqArrayOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_ARRAY));
        long sqFlagsOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, SQ_OFF_FLAGS));

        long cqHeadOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_HEAD));
        long cqTailOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_TAIL));
        long cqRingMaskOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_RING_MASK));
        long cqCqesOff = Integer.toUnsignedLong(params.get(ValueLayout.JAVA_INT, CQ_OFF_CQES));

        // mmap SQ ring
        long sqRingSize = sqArrayOff + sqEntries * 4L;
        MemorySegment sqRing = mmap(sqRingSize, PROT_READ | PROT_WRITE,
                MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_SQ_RING);

        // mmap CQ ring
        long cqRingSize = cqCqesOff + cqEntries * CQE_SIZE;
        MemorySegment cqRing = mmap(cqRingSize, PROT_READ | PROT_WRITE,
                MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_CQ_RING);

        // mmap SQEs
        MemorySegment sqes = mmap(sqEntries * SQE_SIZE, PROT_READ | PROT_WRITE,
                MAP_SHARED | MAP_POPULATE, ringFd, IORING_OFF_SQES);

        int sqMask = sqRing.get(ValueLayout.JAVA_INT, sqRingMaskOff);
        int cqMask = cqRing.get(ValueLayout.JAVA_INT, cqRingMaskOff);

        return new IoUringRing(ringFd, sqEntries,
                sqRing, cqRing, sqes,
                sqMask, cqMask,
                sqHeadOff, sqTailOff, sqArrayOff,
                cqHeadOff, cqTailOff, cqCqesOff,
                sqpoll, sqFlagsOff);
    }

    /**
     * Get the next available SQE, or null if the submission queue is full.
     * In SQPOLL mode, uses local tail (not yet published to kernel).
     */
    public MemorySegment getSqe() {
        int tail;
        int head;

        if (sqpollEnabled) {
            tail = localTail;
            VarHandle.loadLoadFence();
            head = sqRing.get(ValueLayout.JAVA_INT, sqHeadOff);
        } else {
            tail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
            head = sqRing.get(ValueLayout.JAVA_INT, sqHeadOff);
        }

        if (tail - head >= entries) {
            return null;
        }

        int index = tail & sqMask;
        sqRing.set(ValueLayout.JAVA_INT, sqArrayOff + index * 4L, index);

        if (sqpollEnabled) {
            localTail = tail + 1;
        } else {
            sqRing.set(ValueLayout.JAVA_INT, sqTailOff, tail + 1);
        }

        return sqes.asSlice(index * SQE_SIZE, SQE_SIZE);
    }

    /**
     * Submit queued SQEs to the kernel.
     * - Standard mode: calls io_uring_enter with count of pending SQEs.
     * - SQPOLL mode: publishes tail with store fence, wakes SQPOLL thread if sleeping.
     */
    public void submit() throws Throwable {
        if (sqpollEnabled) {
            // Ensure all SQE writes are visible before publishing tail
            VarHandle.storeStoreFence();
            sqRing.set(ValueLayout.JAVA_INT, sqTailOff, localTail);
            VarHandle.storeStoreFence();

            // Check if SQPOLL thread went to sleep and needs waking
            VarHandle.loadLoadFence();
            int sqFlags = sqRing.get(ValueLayout.JAVA_INT, sqFlagsOff);
            if ((sqFlags & IORING_SQ_NEED_WAKEUP) != 0) {
                ioUringEnterRaw(ringFd, 0, 0, IORING_ENTER_SQ_WAKEUP);
            }
        } else {
            int tail = sqRing.get(ValueLayout.JAVA_INT, sqTailOff);
            int head = sqRing.get(ValueLayout.JAVA_INT, sqHeadOff);
            int toSubmit = tail - head;
            if (toSubmit > 0) {
                ioUringEnter(ringFd, toSubmit, 0, 0);
            }
        }
    }

    /**
     * Wait for at least minComplete CQEs to become available (blocking).
     */
    public void waitForCompletions(int minComplete) throws Throwable {
        ioUringEnter(ringFd, 0, minComplete, IORING_ENTER_GETEVENTS);
    }

    /**
     * Non-blocking peek: ask kernel to make CQEs available without waiting.
     * Returns immediately even if no CQEs are ready.
     */
    public void peekCompletions() throws Throwable {
        ioUringEnterRaw(ringFd, 0, 0, IORING_ENTER_GETEVENTS);
    }

    /**
     * Iterate over available CQEs and invoke the consumer for each.
     * Advances the CQ head after processing.
     *
     * @return true if at least one CQE was processed
     */
    public boolean forEachCqe(CqeConsumer consumer) {
        VarHandle.loadLoadFence();
        int cqHead = cqRing.get(ValueLayout.JAVA_INT, cqHeadOff);
        int cqTail = cqRing.get(ValueLayout.JAVA_INT, cqTailOff);

        if (cqHead == cqTail) {
            return false;
        }

        while (cqHead != cqTail) {
            int index = cqHead & cqMask;
            long baseOff = cqCqesOff + (long) index * CQE_SIZE;

            long userData = cqRing.get(ValueLayout.JAVA_LONG, baseOff + CQE_USER_DATA_OFF);
            int res = cqRing.get(ValueLayout.JAVA_INT, baseOff + CQE_RES_OFF);
            int cqeFlags = cqRing.get(ValueLayout.JAVA_INT, baseOff + CQE_FLAGS_OFF);

            consumer.accept(userData, res, cqeFlags);
            cqHead++;
        }

        cqRing.set(ValueLayout.JAVA_INT, cqHeadOff, cqHead);
        VarHandle.storeStoreFence();
        return true;
    }

    /**
     * Register fixed buffers with this ring.
     */
    public void registerBuffers(MemorySegment iovecArray, int count) throws Throwable {
        ioUringRegister(ringFd, IORING_REGISTER_BUFFERS, iovecArray, count);
        buffersRegistered = true;
    }

    public int fd() {
        return ringFd;
    }

    public boolean isSqpoll() {
        return sqpollEnabled;
    }

    @Override
    public void close() throws Exception {
        if (buffersRegistered) {
            try {
                ioUringRegister(ringFd, IORING_UNREGISTER_BUFFERS, null, 0);
            } catch (Throwable t) { /* best effort */ }
        }
        try {
            IoUringNative.close(ringFd);
        } catch (Throwable t) { /* best effort */ }
    }

    @FunctionalInterface
    public interface CqeConsumer {
        void accept(long userData, int res, int cqeFlags);
    }
}
