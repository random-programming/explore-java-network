#!/usr/bin/env bash
set -euo pipefail

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# Collect system metrics in background during a benchmark run.
# Usage: collect_metrics.sh <server_pid> <client_pid> <duration_sec> <output_dir>

SERVER_PID="${1:?Usage: collect_metrics.sh <server_pid> <client_pid> <duration_sec> <output_dir>}"
CLIENT_PID="${2:?}"
DURATION="${3:?}"
OUTPUT_DIR="${4:?}"

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

# Find actual Java server child process (Gradle spawns Java as child)
find_java_pid() {
    local parent_pid="$1"
    local java_pid
    java_pid=$(pgrep -f "java.*benchmark\.server" -P "$parent_pid" 2>/dev/null | head -1)
    if [ -z "$java_pid" ]; then
        for child in $(pgrep -P "$parent_pid" 2>/dev/null); do
            java_pid=$(pgrep -f "java.*benchmark\.server" -P "$child" 2>/dev/null | head -1)
            [ -n "$java_pid" ] && break
            for grandchild in $(pgrep -P "$child" 2>/dev/null); do
                java_pid=$(pgrep -f "java" -P "$grandchild" 2>/dev/null | head -1)
                [ -n "$java_pid" ] && break 2
            done
        done
    fi
    if [ -z "$java_pid" ]; then
        echo "$parent_pid"
    else
        echo "$java_pid"
    fi
}

# Wait a moment for Java to start, then find real PID
sleep 1
REAL_SERVER_PID=$(find_java_pid "$SERVER_PID")
echo "Server PID: $SERVER_PID -> Java PID: $REAL_SERVER_PID"

# Run Java MetricsCollector (per-second CSV: cpu, context switches, memory, fd, strace)
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" :collector:run --args="$REAL_SERVER_PID $CLIENT_PID $DURATION $OUTPUT_DIR" --quiet 2>/dev/null &
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
