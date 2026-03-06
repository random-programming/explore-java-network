plugins {
    java
    application
}

dependencies {
    // No Netty - pure FFM implementation, no external dependencies
}

application {
    mainClass.set("benchmark.server.iouring.ffm.IoUringFfmServer")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED",
            "-Xms256m", "-Xmx2g",
            "-XX:+UseG1GC", "-XX:+AlwaysPreTouch")
}

// Task to run the JNI vs FFM benchmark
tasks.register<JavaExec>("benchmark") {
    classpath = sourceSets["main"].runtimeClasspath
    mainClass.set("benchmark.server.iouring.ffm.JniVsFfmBenchmark")
    jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED")
}
