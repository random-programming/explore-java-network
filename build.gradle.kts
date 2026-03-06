plugins {
    java
    application
}

allprojects {
    group = "benchmark"
    version = "1.0.0"

    repositories {
        mavenCentral()
    }
}

subprojects {
    apply(plugin = "java")

    java {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }

    tasks.withType<JavaCompile> {
        options.encoding = "UTF-8"
        options.compilerArgs.addAll(listOf("--enable-preview"))
    }

    tasks.withType<JavaExec> {
        jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED")
    }

    tasks.withType<Test> {
        jvmArgs("--enable-preview", "--enable-native-access=ALL-UNNAMED")
    }
}

val nettyVersion = "4.1.114.Final"
val nettyIoUringVersion = "0.0.25.Final"
val hdrHistogramVersion = "2.2.2"

// Shared dependency versions accessible to subprojects
ext {
    set("nettyVersion", nettyVersion)
    set("nettyIoUringVersion", nettyIoUringVersion)
    set("hdrHistogramVersion", hdrHistogramVersion)
}
