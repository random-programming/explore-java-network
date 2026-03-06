plugins {
    java
    application
}

val nettyVersion = rootProject.ext["nettyVersion"] as String

dependencies {
    implementation("io.netty:netty-transport:$nettyVersion")
    implementation("io.netty:netty-codec-http:$nettyVersion")
    implementation("io.netty:netty-handler:$nettyVersion")
    implementation("io.netty:netty-transport-native-epoll:$nettyVersion:linux-x86_64")
    implementation("io.netty:netty-transport-native-epoll:$nettyVersion:linux-aarch_64")
}

application {
    mainClass.set("benchmark.server.epoll.EpollServer")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview", "-Xms256m", "-Xmx2g",
            "-XX:+UseG1GC", "-XX:+AlwaysPreTouch")
}
