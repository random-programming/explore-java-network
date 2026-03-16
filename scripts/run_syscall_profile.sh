#!/usr/bin/env bash
set -euo pipefail

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# Syscall profiling pass — separate from throughput measurement.
# Uses perf trace (perf_events, NOT ptrace) — minimal overhead, works with all models including FFM-MT.
#
# 5 models x 3 conn levels (1, 100, 1000) x 1 size (4KB) = 15 tests
# Results go to results_v5.3_syscalls/

TG_BOT_TOKEN="8772896317:AAGgI1QYLSjkmDfOtMOkB3uNwok09uz4kJQ"
TG_CHAT_ID="731427851"

send_telegram() {
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TG_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="${message}" >/dev/null 2>&1 || true
}

PORT="${1:-8080}"
BENCHMARK_DIR="/ssd/benchmark"
RESULTS_DIR="$BENCHMARK_DIR/results_v5.3_syscalls"
mkdir -p "$RESULTS_DIR"

MODELS=(blocking nio epoll iouring iouring-ffm-mt)
CONNECTIONS=(1 100 1000)
DATA_SIZE=4096
CPU_CONFIG="4c"
SERVER_CPUS="0,1"
CLIENT_CPUS="2,3"
PROFILE_DURATION=10  # seconds of perf trace

# Model -> Gradle module mapping
module_for_model() {
    case "$1" in
        blocking) echo "servers:blocking-server" ;;
        nio)      echo "servers:nio-server" ;;
        epoll)    echo "servers:epoll-server" ;;
        iouring)  echo "servers:iouring-server" ;;
        iouring-ffm-mt) echo "servers:iouring-ffm-mt" ;;
    esac
}

TOTAL=$(( ${#MODELS[@]} * ${#CONNECTIONS[@]} ))
CURRENT=0
FAILED=0

echo "================================================================"
echo "Syscall Profiling Pass (perf trace)"
echo "Models: ${MODELS[*]}"
echo "Connections: ${CONNECTIONS[*]}"
echo "Data size: ${DATA_SIZE}B"
echo "CPU config: $CPU_CONFIG"
echo "Profile duration: ${PROFILE_DURATION}s"
echo "Total: $TOTAL tests"
echo "Results: $RESULTS_DIR"
echo "================================================================"
echo ""

send_telegram "🔬 *Syscall profiling started*
Tests: ${TOTAL} | Duration: ${PROFILE_DURATION}s each
Models: ${MODELS[*]}
Connections: ${CONNECTIONS[*]}"

# Build everything first
echo "Building project..."
cd "$BENCHMARK_DIR"
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" build -x test --quiet 2>/dev/null || true
echo ""

# Don't kill Gradle daemons — restarting daemon adds 5-10s to each server start
# and causes wrapper PID to exit before server is ready

for model in "${MODELS[@]}"; do
    SERVER_MODULE=$(module_for_model "$model")

    for conns in "${CONNECTIONS[@]}"; do
        CURRENT=$((CURRENT + 1))
        TEST_NAME="${model}_${CPU_CONFIG}_${conns}conn_${DATA_SIZE}"
        TEST_DIR="$RESULTS_DIR/$TEST_NAME"

        # Skip if already done
        if [ -f "$TEST_DIR/perf_trace.txt" ]; then
            echo "[$CURRENT/$TOTAL] SKIP (done): $TEST_NAME"
            continue
        fi

        mkdir -p "$TEST_DIR"
        echo "[$CURRENT/$TOTAL] $TEST_NAME"

        # Start server
        taskset -c "$SERVER_CPUS" "$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" ":${SERVER_MODULE}:run" --args="$PORT" --quiet &>/dev/null &
        SERVER_PID=$!

        # Wait for server to start listening (wrapper may exit before server is ready)
        REAL_PID=""
        for i in $(seq 1 15); do
            REAL_PID=$(ss -tlnp | grep ":${PORT} " | grep -oP 'pid=\K\d+' | head -1 || true)
            if [ -n "$REAL_PID" ]; then
                break
            fi
            sleep 1
        done
        if [ -z "$REAL_PID" ]; then
            echo "  ERROR: Server failed to start (not listening on port $PORT after 15s)"
            kill "$SERVER_PID" 2>/dev/null || true
            kill -9 "$SERVER_PID" 2>/dev/null || true
            FAILED=$((FAILED + 1))
            sleep 2
            continue
        fi
        echo "  Server PID: $REAL_PID"

        # Start client to generate load (warmup 3s + profile duration)
        CLIENT_DURATION=$((PROFILE_DURATION + 5))
        taskset -c "$CLIENT_CPUS" "$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" :client:run \
            --args="localhost $PORT $conns $DATA_SIZE 3 $CLIENT_DURATION /tmp/syscall_profile_discard" \
            --quiet &>/dev/null &
        CLIENT_PID=$!

        # Wait for warmup
        sleep 5

        # Run perf trace
        echo "  Running perf trace for ${PROFILE_DURATION}s..."
        perf trace -s -p "$REAL_PID" -- sleep "$PROFILE_DURATION" > "$TEST_DIR/perf_trace.txt" 2>&1 || true

        # Stop client and server (kill real PIDs, not just wrappers)
        kill "$REAL_PID" 2>/dev/null || true
        kill "$CLIENT_PID" 2>/dev/null || true
        kill "$SERVER_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$REAL_PID" 2>/dev/null || true
        kill -9 "$CLIENT_PID" 2>/dev/null || true
        kill -9 "$SERVER_PID" 2>/dev/null || true
        wait "$CLIENT_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true

        # Check result
        if [ -s "$TEST_DIR/perf_trace.txt" ]; then
            LINES=$(wc -l < "$TEST_DIR/perf_trace.txt")
            echo "  OK: perf_trace.txt ($LINES lines)"
        else
            echo "  WARNING: perf_trace.txt is empty"
            FAILED=$((FAILED + 1))
        fi

        # Cleanup temp client results
        rm -rf /tmp/syscall_profile_discard 2>/dev/null || true

        sleep 3
        echo ""
    done
done

send_telegram "✅ *Syscall profiling complete*
Total: ${TOTAL} | Failed: ${FAILED}
Results: $RESULTS_DIR"

echo "================================================================"
echo "Syscall profiling complete!"
echo "Total: $TOTAL | Failed: $FAILED"
echo "Results: $RESULTS_DIR"
echo "================================================================"
