#!/usr/bin/env bash
set -euo pipefail

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# Telegram notification
TG_BOT_TOKEN="8772896317:AAGgI1QYLSjkmDfOtMOkB3uNwok09uz4kJQ"
TG_CHAT_ID="731427851"

send_telegram() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TG_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="${message}" >/dev/null 2>&1 || true
}

# Run a single benchmark test configuration.
# Usage: run_single_test.sh <model> <port> <connections> <data_size> <cpu_config> <run_number> [warmup] [duration]
#
# model:       blocking | nio | epoll | iouring
# cpu_config:  1c | 4c | 8c
# Example:     run_single_test.sh epoll 8080 100 4096 4c 1

MODEL="${1:?Usage: run_single_test.sh <model> <port> <connections> <data_size> <cpu_config> <run_number>}"
PORT="${2:?}"
CONNECTIONS="${3:?}"
DATA_SIZE="${4:?}"
CPU_CONFIG="${5:?}"
RUN_NUMBER="${6:?}"
WARMUP="${7:-5}"
DURATION="${8:-30}"
PAUSE="${9:-5}"
MAX_TEST_TIME="${10:-120}"  # Safety timeout per test (seconds)

BENCHMARK_DIR="/ssd/benchmark"
RESULTS_BASE="${RESULTS_BASE_DIR:-$BENCHMARK_DIR/results}"
RESULTS_DIR="$RESULTS_BASE/${MODEL}_${CPU_CONFIG}_${CONNECTIONS}conn_${DATA_SIZE}_run${RUN_NUMBER}"
mkdir -p "$RESULTS_DIR"

# Determine CPU pinning
case "$CPU_CONFIG" in
    1c)
        SERVER_CPUS="0"
        CLIENT_CPUS="0"
        ;;
    4c)
        SERVER_CPUS="0,1"
        CLIENT_CPUS="2,3"
        ;;
    8c)
        SERVER_CPUS="0-3"
        CLIENT_CPUS="4-7"
        ;;
    *)
        echo "Unknown CPU config: $CPU_CONFIG (use 1c, 4c, 8c)"
        exit 1
        ;;
esac

# Determine server module
case "$MODEL" in
    blocking) SERVER_MODULE="servers:blocking-server" ;;
    nio)      SERVER_MODULE="servers:nio-server" ;;
    epoll)    SERVER_MODULE="servers:epoll-server" ;;
    iouring)  SERVER_MODULE="servers:iouring-server" ;;
    iouring-ffm) SERVER_MODULE="servers:iouring-ffm-demo" ;;
    iouring-ffm-mt) SERVER_MODULE="servers:iouring-ffm-mt" ;;
    *)
        echo "Unknown model: $MODEL (use blocking, nio, epoll, iouring, iouring-ffm, iouring-ffm-mt)"
        exit 1
        ;;
esac

echo "================================================================"
echo "Test: model=$MODEL conns=$CONNECTIONS size=$DATA_SIZE cpu=$CPU_CONFIG run=$RUN_NUMBER"
echo "  Server CPUs: $SERVER_CPUS  Client CPUs: $CLIENT_CPUS"
echo "  Warmup: ${WARMUP}s  Duration: ${DURATION}s"
echo "  Output: $RESULTS_DIR"
echo "================================================================"

# Start server with CPU pinning
echo "Starting $MODEL server on port $PORT..."
cd "$BENCHMARK_DIR"
taskset -c "$SERVER_CPUS" "$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" :${SERVER_MODULE}:run --args="$PORT" --quiet &>/dev/null &
SERVER_PID=$!
sleep 2

# Verify server is running
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: Server failed to start!"
    exit 1
fi

echo "Server PID: $SERVER_PID"

# Start client with CPU pinning
TOTAL_DURATION=$((WARMUP + DURATION))
echo "Starting client: connections=$CONNECTIONS size=$DATA_SIZE warmup=${WARMUP}s duration=${DURATION}s"
taskset -c "$CLIENT_CPUS" "$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" :client:run \
    --args="localhost $PORT $CONNECTIONS $DATA_SIZE $WARMUP $DURATION $RESULTS_DIR" \
    --quiet &>/dev/null &
CLIENT_PID=$!
sleep 1

echo "Client PID: $CLIENT_PID"

# Start metrics collector (skip warmup, measure only active phase)
echo "Starting metrics collector..."
sleep "$WARMUP"
# Disable strace for FFM models — ptrace attachment crashes io_uring FFM servers
NO_STRACE=""
case "$MODEL" in
    iouring-ffm|iouring-ffm-mt) NO_STRACE="1" ;;
esac
"$BENCHMARK_DIR/scripts/collect_metrics.sh" "$SERVER_PID" "$CLIENT_PID" "$DURATION" "$RESULTS_DIR" "$PORT" "$NO_STRACE" &
COLLECTOR_PID=$!

# Wait for client to finish, with safety timeout
SECONDS=0
while kill -0 "$CLIENT_PID" 2>/dev/null; do
    if [ "$SECONDS" -ge "$MAX_TEST_TIME" ]; then
        echo "WARNING: Test exceeded ${MAX_TEST_TIME}s timeout, force killing..."
        break
    fi
    sleep 1
done
echo "Client finished (${SECONDS}s)."

# Wait for collector to finish (it needs time to run perf sched latency)
echo "Waiting for metrics collector to finish..."
COLLECTOR_WAIT=0
while kill -0 "$COLLECTOR_PID" 2>/dev/null; do
    if [ "$COLLECTOR_WAIT" -ge 30 ]; then
        echo "WARNING: Collector timeout, force killing..."
        kill -9 "$COLLECTOR_PID" 2>/dev/null || true
        break
    fi
    sleep 1
    COLLECTOR_WAIT=$((COLLECTOR_WAIT + 1))
done
wait "$COLLECTOR_PID" 2>/dev/null || true
echo "Collector finished."

# Now kill client and server
kill "$CLIENT_PID" 2>/dev/null || true
kill "$SERVER_PID" 2>/dev/null || true
sleep 1
kill -9 "$CLIENT_PID" 2>/dev/null || true
kill -9 "$SERVER_PID" 2>/dev/null || true
wait "$CLIENT_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

# Build report from CSV
REPORT="*Test complete*
Model: \`${MODEL}\` | CPU: \`${CPU_CONFIG}\` | Conns: \`${CONNECTIONS}\` | Size: \`${DATA_SIZE}B\` | Run: \`${RUN_NUMBER}\`
Time: ${SECONDS}s"

# Add throughput summary if available
if [ -f "$RESULTS_DIR/throughput.csv" ]; then
    AVG_RPS=$(tail -n +2 "$RESULTS_DIR/throughput.csv" | awk -F',' '{sum+=$4; n++} END {if(n>0) printf "%.0f", sum/n; else print "N/A"}')
    REPORT="${REPORT}
RPS (avg): \`${AVG_RPS}\`"
fi

# Add latency summary if available
if [ -f "$RESULTS_DIR/latency.csv" ]; then
    LAT_P50=$(tail -n +2 "$RESULTS_DIR/latency.csv" | awk -F',' '{sum+=$2; n++} END {if(n>0) printf "%.0f", sum/n; else print "N/A"}')
    LAT_P99=$(tail -n +2 "$RESULTS_DIR/latency.csv" | awk -F',' '{sum+=$4; n++} END {if(n>0) printf "%.0f", sum/n; else print "N/A"}')
    REPORT="${REPORT}
Latency p50: \`${LAT_P50}us\` | p99: \`${LAT_P99}us\`"
fi

send_telegram "$REPORT"

echo "Test complete. Results in: $RESULTS_DIR"
echo "Pausing ${PAUSE}s..."
sleep "$PAUSE"
