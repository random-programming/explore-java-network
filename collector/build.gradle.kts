plugins {
    java
    application
}

application {
    mainClass.set("benchmark.collector.MetricsCollector")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview")
}
