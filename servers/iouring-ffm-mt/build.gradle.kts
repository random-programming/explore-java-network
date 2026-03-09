plugins {
    java
    application
}

dependencies {
    // No Netty - pure FFM implementation, no external dependencies
}

application {
    mainClass.set("benchmark.server.iouring.ffm.mt.IoUringFfmMtServer")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED",
            "-Xms256m", "-Xmx2g",
            "-XX:+UseG1GC", "-XX:+AlwaysPreTouch")
}
