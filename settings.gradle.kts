rootProject.name = "io-benchmark"

include("servers:blocking-server")
include("servers:nio-server")
include("servers:epoll-server")
include("servers:iouring-server")
include("servers:iouring-ffm-demo")
include("servers:iouring-ffm-mt")
include("client")
include("collector")
