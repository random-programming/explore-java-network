plugins {
    java
    application
}

val nettyVersion = rootProject.ext["nettyVersion"] as String
val nettyIoUringVersion = rootProject.ext["nettyIoUringVersion"] as String

dependencies {
    implementation("io.netty:netty-transport:$nettyVersion")
    implementation("io.netty:netty-codec-http:$nettyVersion")
    implementation("io.netty:netty-handler:$nettyVersion")
    implementation("io.netty.incubator:netty-incubator-transport-native-io_uring:$nettyIoUringVersion:linux-x86_64")
    implementation("io.netty.incubator:netty-incubator-transport-native-io_uring:$nettyIoUringVersion:linux-aarch_64")
}

application {
    mainClass.set("benchmark.server.iouring.IoUringServer")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED",
            "-Xms256m", "-Xmx2g",
            "-XX:+UseG1GC", "-XX:+AlwaysPreTouch")
}
