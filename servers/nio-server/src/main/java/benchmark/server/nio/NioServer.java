package benchmark.server.nio;

import io.netty.bootstrap.ServerBootstrap;
import io.netty.buffer.ByteBuf;
import io.netty.buffer.Unpooled;
import io.netty.channel.*;
import io.netty.channel.nio.NioEventLoopGroup;
import io.netty.channel.socket.SocketChannel;
import io.netty.channel.socket.nio.NioServerSocketChannel;
import io.netty.handler.codec.http.*;

import java.nio.charset.StandardCharsets;
import java.util.*;

public class NioServer {

    private static final int[] SUPPORTED_SIZES = {64, 512, 4096, 16384, 65536, 131072, 524288, 1048576};
    private static final Map<Integer, ByteBuf> DATA_BUFFERS = new HashMap<>();

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        int threads = args.length > 1 ? Integer.parseInt(args[1]) : Runtime.getRuntime().availableProcessors();

        // Pre-generate data buffers (unreleasable so they persist)
        Random random = new Random(42);
        for (int size : SUPPORTED_SIZES) {
            byte[] buf = new byte[size];
            random.nextBytes(buf);
            DATA_BUFFERS.put(size, Unpooled.unreleasableBuffer(Unpooled.directBuffer(size).writeBytes(buf)));
        }

        EventLoopGroup bossGroup = new NioEventLoopGroup(1);
        EventLoopGroup workerGroup = new NioEventLoopGroup(threads);

        try {
            ServerBootstrap bootstrap = new ServerBootstrap();
            bootstrap.group(bossGroup, workerGroup)
                    .channel(NioServerSocketChannel.class)
                    .option(ChannelOption.SO_BACKLOG, 65535)
                    .option(ChannelOption.SO_REUSEADDR, true)
                    .childOption(ChannelOption.TCP_NODELAY, true)
                    .childOption(ChannelOption.SO_KEEPALIVE, false)
                    .childHandler(new ChannelInitializer<SocketChannel>() {
                        @Override
                        protected void initChannel(SocketChannel ch) {
                            ch.pipeline().addLast(
                                    new HttpServerCodec(),
                                    new HttpObjectAggregator(1024),
                                    new BenchmarkHandler()
                            );
                        }
                    });

            ChannelFuture future = bootstrap.bind(port).sync();
            System.out.println("Server started on port " + port + " with " + threads + " threads (nio)");

            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                bossGroup.shutdownGracefully();
                workerGroup.shutdownGracefully();
                System.out.println("Server stopped.");
            }));

            future.channel().closeFuture().sync();
        } finally {
            bossGroup.shutdownGracefully();
            workerGroup.shutdownGracefully();
        }
    }

    private static class BenchmarkHandler extends SimpleChannelInboundHandler<FullHttpRequest> {
        @Override
        protected void channelRead0(ChannelHandlerContext ctx, FullHttpRequest request) {
            int size = parseSize(request.uri());
            ByteBuf data = DATA_BUFFERS.getOrDefault(size, DATA_BUFFERS.get(64)).retainedDuplicate();

            DefaultFullHttpResponse response = new DefaultFullHttpResponse(
                    HttpVersion.HTTP_1_1, HttpResponseStatus.OK, data);
            response.headers().set(HttpHeaderNames.CONTENT_LENGTH, data.readableBytes());
            response.headers().set(HttpHeaderNames.CONTENT_TYPE, "application/octet-stream");
            response.headers().set(HttpHeaderNames.CONNECTION, HttpHeaderValues.CLOSE);

            ctx.writeAndFlush(response).addListener(ChannelFutureListener.CLOSE);
        }

        @Override
        public void exceptionCaught(ChannelHandlerContext ctx, Throwable cause) {
            ctx.close();
        }

        private static int parseSize(String uri) {
            try {
                int idx = uri.indexOf("size=");
                if (idx == -1) return 64;
                int start = idx + 5;
                int end = start;
                while (end < uri.length() && Character.isDigit(uri.charAt(end))) {
                    end++;
                }
                return Integer.parseInt(uri.substring(start, end));
            } catch (Exception e) {
                return 64;
            }
        }
    }
}
