package benchmark.server.iouring.ffm.mt;

import java.lang.foreign.*;
import java.lang.invoke.MethodHandle;

/**
 * Low-level FFM bindings for Linux io_uring.
 * Extended version with SQPOLL, multishot accept, and fixed buffer support.
 */
public final class IoUringNative {

    private IoUringNative() {}

    // ── Syscall numbers (x86_64) ──
    public static final int __NR_io_uring_setup    = 425;
    public static final int __NR_io_uring_enter    = 426;
    public static final int __NR_io_uring_register = 427;

    // ── io_uring_setup flags ──
    public static final int IORING_SETUP_SQPOLL    = 1 << 1;
    public static final int IORING_SETUP_SQ_AFF    = 1 << 2;
    public static final int IORING_SETUP_CQSIZE    = 1 << 3;

    // ── io_uring_enter flags ──
    public static final int IORING_ENTER_GETEVENTS = 1 << 0;
    public static final int IORING_ENTER_SQ_WAKEUP = 1 << 1;

    // ── io_uring_register opcodes ──
    public static final int IORING_REGISTER_BUFFERS   = 0;
    public static final int IORING_UNREGISTER_BUFFERS  = 1;

    // ── SQE opcodes ──
    public static final int IORING_OP_NOP           = 0;
    public static final int IORING_OP_READV          = 1;
    public static final int IORING_OP_WRITEV         = 2;
    public static final int IORING_OP_READ_FIXED     = 4;
    public static final int IORING_OP_WRITE_FIXED    = 5;
    public static final int IORING_OP_ACCEPT         = 13;
    public static final int IORING_OP_RECV           = 27;
    public static final int IORING_OP_SEND           = 26;
    public static final int IORING_OP_CLOSE          = 19;

    // ── Accept flags ──
    public static final int IORING_ACCEPT_MULTISHOT  = 1 << 0;

    // ── CQE flags ──
    public static final int IORING_CQE_F_MORE       = 1 << 1;

    // ── SQ ring flags (read from sq_ring at sq_off.flags) ──
    public static final int IORING_SQ_NEED_WAKEUP   = 1 << 0;

    // ── Socket constants ──
    public static final int AF_INET       = 2;
    public static final int SOCK_STREAM   = 1;
    public static final int SOL_SOCKET    = 1;
    public static final int SO_REUSEADDR  = 2;
    public static final int SO_REUSEPORT  = 15;
    public static final int IPPROTO_TCP   = 6;
    public static final int TCP_NODELAY   = 1;

    // ── Offsets within io_uring_sqe (64 bytes) ──
    public static final long SQE_SIZE = 64;
    public static final long SQE_OPCODE_OFF    = 0;
    public static final long SQE_FLAGS_OFF     = 1;
    public static final long SQE_IOPRIO_OFF    = 2;
    public static final long SQE_FD_OFF        = 4;
    public static final long SQE_OFF_OFF       = 8;
    public static final long SQE_ADDR_OFF      = 16;
    public static final long SQE_LEN_OFF       = 24;
    public static final long SQE_RW_FLAGS_OFF  = 28;
    public static final long SQE_USER_DATA_OFF = 32;
    public static final long SQE_BUF_INDEX_OFF = 40;

    // ── Offsets within io_uring_cqe (16 bytes) ──
    public static final long CQE_SIZE = 16;
    public static final long CQE_USER_DATA_OFF = 0;
    public static final long CQE_RES_OFF       = 8;
    public static final long CQE_FLAGS_OFF     = 12;

    // ── io_uring_params offsets (total 120 bytes) ──
    public static final long PARAMS_SIZE             = 120;
    public static final long PARAMS_SQ_ENTRIES_OFF   = 0;
    public static final long PARAMS_CQ_ENTRIES_OFF   = 4;
    public static final long PARAMS_FLAGS_OFF        = 8;
    public static final long PARAMS_SQ_THREAD_CPU_OFF = 12;
    public static final long PARAMS_SQ_THREAD_IDLE_OFF = 16;
    public static final long PARAMS_FEATURES_OFF     = 20;

    // sq_off offsets (relative to params + 40)
    public static final long SQ_OFF_BASE     = 40;
    public static final long SQ_OFF_HEAD     = SQ_OFF_BASE + 0;
    public static final long SQ_OFF_TAIL     = SQ_OFF_BASE + 4;
    public static final long SQ_OFF_RING_MASK = SQ_OFF_BASE + 8;
    public static final long SQ_OFF_RING_ENTRIES = SQ_OFF_BASE + 12;
    public static final long SQ_OFF_FLAGS    = SQ_OFF_BASE + 16;
    public static final long SQ_OFF_DROPPED  = SQ_OFF_BASE + 20;
    public static final long SQ_OFF_ARRAY    = SQ_OFF_BASE + 24;

    // cq_off offsets (relative to params + 80)
    public static final long CQ_OFF_BASE     = 80;
    public static final long CQ_OFF_HEAD     = CQ_OFF_BASE + 0;
    public static final long CQ_OFF_TAIL     = CQ_OFF_BASE + 4;
    public static final long CQ_OFF_RING_MASK = CQ_OFF_BASE + 8;
    public static final long CQ_OFF_RING_ENTRIES = CQ_OFF_BASE + 12;
    public static final long CQ_OFF_OVERFLOW = CQ_OFF_BASE + 16;
    public static final long CQ_OFF_CQES     = CQ_OFF_BASE + 20;

    // ── mmap offsets for io_uring ──
    public static final long IORING_OFF_SQ_RING = 0L;
    public static final long IORING_OFF_CQ_RING = 0x8000000L;
    public static final long IORING_OFF_SQES    = 0x10000000L;

    // ── mmap constants ──
    public static final int PROT_READ  = 0x1;
    public static final int PROT_WRITE = 0x2;
    public static final int MAP_SHARED = 0x01;
    public static final int MAP_POPULATE = 0x08000;

    // ── iovec layout: { void* iov_base; size_t iov_len; } = 16 bytes on x86_64 ──
    public static final long IOVEC_SIZE = 16;
    public static final long IOVEC_BASE_OFF = 0;
    public static final long IOVEC_LEN_OFF  = 8;

    // ── Method handles for syscalls ──
    private static final Linker LINKER = Linker.nativeLinker();
    private static final SymbolLookup LOOKUP = LINKER.defaultLookup();

    private static final MethodHandle SYSCALL;

    static {
        MemorySegment syscallAddr = SymbolLookup.loaderLookup()
                .or(LINKER.defaultLookup())
                .find("syscall")
                .orElseThrow(() -> new UnsupportedOperationException("syscall not found"));

        SYSCALL = LINKER.downcallHandle(
                syscallAddr,
                FunctionDescriptor.of(
                        ValueLayout.JAVA_LONG,  // return
                        ValueLayout.JAVA_LONG,  // syscall number
                        ValueLayout.JAVA_LONG,  // arg1
                        ValueLayout.JAVA_LONG,  // arg2
                        ValueLayout.JAVA_LONG,  // arg3
                        ValueLayout.JAVA_LONG,  // arg4
                        ValueLayout.JAVA_LONG   // arg5
                ),
                Linker.Option.firstVariadicArg(1)
        );
    }

    // ── Syscall wrappers ──

    public static int ioUringSetup(int entries, MemorySegment params) throws Throwable {
        long result = (long) SYSCALL.invokeExact(
                (long) __NR_io_uring_setup,
                (long) entries,
                params.address(),
                0L, 0L, 0L
        );
        if (result < 0) {
            throw new RuntimeException("io_uring_setup failed: errno=" + (-result));
        }
        return (int) result;
    }

    public static int ioUringEnter(int fd, int toSubmit, int minComplete, int flags) throws Throwable {
        long result = (long) SYSCALL.invokeExact(
                (long) __NR_io_uring_enter,
                (long) fd,
                (long) toSubmit,
                (long) minComplete,
                (long) flags,
                0L  // sig = NULL
        );
        if (result < 0) {
            throw new RuntimeException("io_uring_enter failed: errno=" + (-result));
        }
        return (int) result;
    }

    /**
     * Non-throwing variant for SQPOLL usage. Returns negative errno on failure.
     */
    public static int ioUringEnterRaw(int fd, int toSubmit, int minComplete, int flags) throws Throwable {
        long result = (long) SYSCALL.invokeExact(
                (long) __NR_io_uring_enter,
                (long) fd,
                (long) toSubmit,
                (long) minComplete,
                (long) flags,
                0L
        );
        return (int) result;
    }

    public static int ioUringRegister(int fd, int opcode, MemorySegment arg, int nrArgs) throws Throwable {
        long result = (long) SYSCALL.invokeExact(
                (long) __NR_io_uring_register,
                (long) fd,
                (long) opcode,
                arg != null ? arg.address() : 0L,
                (long) nrArgs,
                0L
        );
        if (result < 0) {
            throw new RuntimeException("io_uring_register failed: errno=" + (-result));
        }
        return (int) result;
    }

    // ── Socket helpers via FFM ──

    private static final MethodHandle SOCKET_FN;
    private static final MethodHandle BIND_FN;
    private static final MethodHandle LISTEN_FN;
    private static final MethodHandle SETSOCKOPT_FN;
    private static final MethodHandle CLOSE_FN;
    private static final MethodHandle MMAP_FN;
    private static final MethodHandle HTONS_FN;

    static {
        try {
            SymbolLookup lookup = SymbolLookup.loaderLookup().or(LINKER.defaultLookup());

            SOCKET_FN = LINKER.downcallHandle(
                    lookup.find("socket").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_INT,
                            ValueLayout.JAVA_INT, ValueLayout.JAVA_INT, ValueLayout.JAVA_INT));

            BIND_FN = LINKER.downcallHandle(
                    lookup.find("bind").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_INT,
                            ValueLayout.JAVA_INT, ValueLayout.ADDRESS, ValueLayout.JAVA_INT));

            LISTEN_FN = LINKER.downcallHandle(
                    lookup.find("listen").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_INT,
                            ValueLayout.JAVA_INT, ValueLayout.JAVA_INT));

            SETSOCKOPT_FN = LINKER.downcallHandle(
                    lookup.find("setsockopt").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_INT,
                            ValueLayout.JAVA_INT, ValueLayout.JAVA_INT, ValueLayout.JAVA_INT,
                            ValueLayout.ADDRESS, ValueLayout.JAVA_INT));

            CLOSE_FN = LINKER.downcallHandle(
                    lookup.find("close").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_INT, ValueLayout.JAVA_INT));

            MMAP_FN = LINKER.downcallHandle(
                    lookup.find("mmap").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.ADDRESS,
                            ValueLayout.ADDRESS, ValueLayout.JAVA_LONG, ValueLayout.JAVA_INT,
                            ValueLayout.JAVA_INT, ValueLayout.JAVA_INT, ValueLayout.JAVA_LONG));

            HTONS_FN = LINKER.downcallHandle(
                    lookup.find("htons").orElseThrow(),
                    FunctionDescriptor.of(ValueLayout.JAVA_SHORT, ValueLayout.JAVA_SHORT));
        } catch (Exception e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    public static int socket(int domain, int type, int protocol) throws Throwable {
        int fd = (int) SOCKET_FN.invokeExact(domain, type, protocol);
        if (fd < 0) throw new RuntimeException("socket() failed: " + fd);
        return fd;
    }

    public static void bind(int sockfd, MemorySegment addr, int addrLen) throws Throwable {
        int ret = (int) BIND_FN.invokeExact(sockfd, addr, addrLen);
        if (ret < 0) throw new RuntimeException("bind() failed: " + ret);
    }

    public static void listen(int sockfd, int backlog) throws Throwable {
        int ret = (int) LISTEN_FN.invokeExact(sockfd, backlog);
        if (ret < 0) throw new RuntimeException("listen() failed: " + ret);
    }

    public static void setsockopt(int sockfd, int level, int optname, MemorySegment optval, int optlen) throws Throwable {
        int ret = (int) SETSOCKOPT_FN.invokeExact(sockfd, level, optname, optval, optlen);
        if (ret < 0) throw new RuntimeException("setsockopt() failed: " + ret);
    }

    public static void close(int fd) throws Throwable {
        CLOSE_FN.invokeExact(fd);
    }

    public static MemorySegment mmap(long length, int prot, int flags, int fd, long offset) throws Throwable {
        MemorySegment result = (MemorySegment) MMAP_FN.invokeExact(
                MemorySegment.NULL, length, prot, flags, fd, offset);
        if (result.address() == -1L) {
            throw new RuntimeException("mmap() failed");
        }
        return result.reinterpret(length);
    }

    public static short htons(short port) throws Throwable {
        return (short) HTONS_FN.invokeExact(port);
    }

    public static MemorySegment buildSockaddrIn(Arena arena, int port) throws Throwable {
        MemorySegment addr = arena.allocate(16);
        addr.set(ValueLayout.JAVA_SHORT, 0, (short) AF_INET);
        addr.set(ValueLayout.JAVA_SHORT, 2, htons((short) port));
        addr.set(ValueLayout.JAVA_INT, 4, 0); // INADDR_ANY
        return addr;
    }

    // ── SQE helpers ──

    public static void sqeSetOp(MemorySegment sqe, byte opcode, int fd, long addr, int len, long userData) {
        sqe.set(ValueLayout.JAVA_BYTE, SQE_OPCODE_OFF, opcode);
        sqe.set(ValueLayout.JAVA_BYTE, SQE_FLAGS_OFF, (byte) 0);
        sqe.set(ValueLayout.JAVA_SHORT, SQE_IOPRIO_OFF, (short) 0);
        sqe.set(ValueLayout.JAVA_INT, SQE_FD_OFF, fd);
        sqe.set(ValueLayout.JAVA_LONG, SQE_OFF_OFF, 0L);
        sqe.set(ValueLayout.JAVA_LONG, SQE_ADDR_OFF, addr);
        sqe.set(ValueLayout.JAVA_INT, SQE_LEN_OFF, len);
        sqe.set(ValueLayout.JAVA_INT, SQE_RW_FLAGS_OFF, 0);
        sqe.set(ValueLayout.JAVA_LONG, SQE_USER_DATA_OFF, userData);
        // Clear the buf_index/buf_group union area
        sqe.set(ValueLayout.JAVA_SHORT, SQE_BUF_INDEX_OFF, (short) 0);
    }

    public static void sqeSetAccept(MemorySegment sqe, int fd, MemorySegment clientAddr,
                                     MemorySegment addrLen, int flags, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_ACCEPT, fd, clientAddr.address(), 0, userData);
        sqe.set(ValueLayout.JAVA_LONG, SQE_OFF_OFF, addrLen.address());
        sqe.set(ValueLayout.JAVA_INT, SQE_RW_FLAGS_OFF, flags);
    }

    public static void sqeSetAcceptMultishot(MemorySegment sqe, int fd, MemorySegment clientAddr,
                                              MemorySegment addrLen, long userData) {
        sqeSetAccept(sqe, fd, clientAddr, addrLen, IORING_ACCEPT_MULTISHOT, userData);
    }

    public static void sqeSetRecv(MemorySegment sqe, int fd, MemorySegment buf, int len, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_RECV, fd, buf.address(), len, userData);
    }

    public static void sqeSetSend(MemorySegment sqe, int fd, MemorySegment buf, int len, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_SEND, fd, buf.address(), len, userData);
    }

    public static void sqeSetClose(MemorySegment sqe, int fd, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_CLOSE, fd, 0L, 0, userData);
    }

    /**
     * Set up an SQE for IORING_OP_READ_FIXED (read into a registered/fixed buffer).
     */
    public static void sqeSetReadFixed(MemorySegment sqe, int fd, long bufAddr, int len,
                                        int bufIndex, long offset, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_READ_FIXED, fd, bufAddr, len, userData);
        sqe.set(ValueLayout.JAVA_LONG, SQE_OFF_OFF, offset);
        sqe.set(ValueLayout.JAVA_SHORT, SQE_BUF_INDEX_OFF, (short) bufIndex);
    }

    /**
     * Set up an SQE for IORING_OP_WRITE_FIXED (write from a registered/fixed buffer).
     */
    public static void sqeSetWriteFixed(MemorySegment sqe, int fd, long bufAddr, int len,
                                         int bufIndex, long offset, long userData) {
        sqeSetOp(sqe, (byte) IORING_OP_WRITE_FIXED, fd, bufAddr, len, userData);
        sqe.set(ValueLayout.JAVA_LONG, SQE_OFF_OFF, offset);
        sqe.set(ValueLayout.JAVA_SHORT, SQE_BUF_INDEX_OFF, (short) bufIndex);
    }

    /**
     * Build an iovec array for io_uring_register(REGISTER_BUFFERS).
     * Each entry points to a pre-allocated buffer of the given size.
     *
     * @return the iovec MemorySegment (caller must keep arena alive)
     */
    public static MemorySegment buildIovecArray(Arena arena, MemorySegment[] buffers) {
        MemorySegment iovec = arena.allocate(IOVEC_SIZE * buffers.length);
        for (int i = 0; i < buffers.length; i++) {
            long base = IOVEC_SIZE * i;
            iovec.set(ValueLayout.JAVA_LONG, base + IOVEC_BASE_OFF, buffers[i].address());
            iovec.set(ValueLayout.JAVA_LONG, base + IOVEC_LEN_OFF, buffers[i].byteSize());
        }
        return iovec;
    }
}
