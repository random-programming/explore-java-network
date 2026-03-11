#!/usr/bin/env bash
set -euo pipefail

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# Collect system metrics in background during a benchmark run.
# Usage: collect_metrics.sh <server_pid> <client_pid> <duration_sec> <output_dir> [port]

SERVER_PID="${1:?Usage: collect_metrics.sh <server_pid> <client_pid> <duration_sec> <output_dir> [port] [no-strace]}"
CLIENT_PID="${2:?}"
DURATION="${3:?}"
OUTPUT_DIR="${4:?}"
SERVER_PORT="${5:-}"
NO_STRACE="${6:-}"

BENCHMARK_DIR="/ssd/benchmark"

mkdir -p "$OUTPUT_DIR"

echo "Collecting metrics: server=$SERVER_PID client=$CLIENT_PID duration=${DURATION}s -> $OUTPUT_DIR"

PERF_DATA="$OUTPUT_DIR/perf_sched.data"
PERF_REPORT="$OUTPUT_DIR/perf_sched_latency.txt"

# Cleanup perf_sched.data on exit regardless of how script terminates
cleanup_perf() {
    rm -f "$PERF_DATA" 2>/dev/null || true
}
trap cleanup_perf EXIT

# Find actual Java server process.
# Gradle daemon runs as a separate process (not a child of the Gradle wrapper),
# so we find the server by the port it listens on.
find_server_pid() {
    local port="$1"
    local fallback_pid="$2"
    if [ -n "$port" ]; then
        local pid
        pid=$(ss -tlnp | grep ":${port} " | grep -oP 'pid=\K\d+' | head -1)
        if [ -n "$pid" ]; then
            echo "$pid"
            return
        fi
    fi
    echo "$fallback_pid"
}

# Wait for Java server to start (Gradle needs time to compile and launch)
sleep 3
REAL_SERVER_PID=$(find_server_pid "$SERVER_PORT" "$SERVER_PID")
echo "Server PID: $SERVER_PID -> Java PID: $REAL_SERVER_PID"

# Run Java MetricsCollector (per-second CSV: cpu, context switches, memory, fd, strace)
COLLECTOR_ARGS="$REAL_SERVER_PID $CLIENT_PID $DURATION $OUTPUT_DIR"
if [ "$NO_STRACE" = "1" ]; then
    COLLECTOR_ARGS="$COLLECTOR_ARGS no-strace"
    echo "Strace disabled (FFM model)"
fi
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" :collector:run --args="$COLLECTOR_ARGS" --quiet 2>/dev/null &
COLLECTOR_PID=$!

# Run perf sched record for kernel scheduling delays
# Record only the server process (-p), NOT system-wide (-a)
# -a generates 2-4 GB per 30s test and caused disk exhaustion + system hang
perf sched record -p "$REAL_SERVER_PID" -o "$PERF_DATA" -- sleep "$DURATION" 2>/dev/null &
PERF_PID=$!

wait $COLLECTOR_PID 2>/dev/null || true
wait $PERF_PID 2>/dev/null || true

# Parse perf sched latency into readable report
if [ -f "$PERF_DATA" ]; then
    perf sched latency -i "$PERF_DATA" > "$PERF_REPORT" 2>/dev/null || true
    rm -f "$PERF_DATA" 2>/dev/null || true
    if [ -s "$PERF_REPORT" ]; then
        echo "Wrote $PERF_REPORT"
    else
        echo "WARN: perf_sched_latency.txt is empty"
    fi
else
    echo "WARN: perf_sched.data not created"
fi

echo "Metrics collection finished."
