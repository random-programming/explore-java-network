plugins {
    java
    application
}

val hdrHistogramVersion = rootProject.ext["hdrHistogramVersion"] as String

dependencies {
    implementation("org.hdrhistogram:HdrHistogram:$hdrHistogramVersion")
}

application {
    mainClass.set("benchmark.client.BenchmarkClient")
}

tasks.named<JavaExec>("run") {
    jvmArgs("--enable-preview", "-Xms512m", "-Xmx4g",
            "-XX:+UseG1GC", "-XX:+AlwaysPreTouch")
}
