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

# Benchmark v5.3: 5 models, 4c only, all conn, all sizes
# strace DISABLED for ALL models (ptrace overhead degrades throughput 3-13x)
# Syscall profiles collected separately via run_syscall_profile.sh (perf trace)
#
# - 1 run for all connections
# - 2nd run for key points: 100 and 1000 connections
# Total: 5 * 5 * 8 * 1 + 5 * 2 * 8 * 1 = 200 + 80 = 280 tests

PORT="${1:-8080}"
BENCHMARK_DIR="/ssd/benchmark"
SCRIPTS_DIR="$BENCHMARK_DIR/scripts"

MODELS=(blocking nio epoll iouring iouring-ffm-mt)
CONNECTIONS=(1 10 100 1000 10000)
DATA_SIZES=(64 512 4096 16384 65536 131072 524288 1048576)
CPU_CONFIG="4c"

# Connections that get a 2nd run for statistical validation
KEY_CONNECTIONS=(100 1000)

export RESULTS_BASE_DIR="$BENCHMARK_DIR/results_v5.3"
mkdir -p "$RESULTS_BASE_DIR"

# Calculate total: all conn x run1 + key conn x run2
TOTAL=$(( ${#MODELS[@]} * ${#CONNECTIONS[@]} * ${#DATA_SIZES[@]} * 1 \
        + ${#MODELS[@]} * ${#KEY_CONNECTIONS[@]} * ${#DATA_SIZES[@]} * 1 ))
CURRENT=0
SKIPPED=0
FAILED=0
START_TIME=$(date +%s)

# Build test list and count skipped
is_key_conn() {
    local c="$1"
    for kc in "${KEY_CONNECTIONS[@]}"; do
        [ "$c" = "$kc" ] && return 0
    done
    return 1
}

max_runs_for_conn() {
    if is_key_conn "$1"; then
        echo 2
    else
        echo 1
    fi
}

# Count already completed tests (for resume)
for model in "${MODELS[@]}"; do
    for conns in "${CONNECTIONS[@]}"; do
        MAX_RUNS=$(max_runs_for_conn "$conns")
        for size in "${DATA_SIZES[@]}"; do
            for run in $(seq 1 "$MAX_RUNS"); do
                RESULTS_DIR="$RESULTS_BASE_DIR/${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run}"
                if [ -f "$RESULTS_DIR/throughput.csv" ] && [ -f "$RESULTS_DIR/latency.csv" ]; then
                    SKIPPED=$((SKIPPED + 1))
                fi
            done
        done
    done
done
REMAINING_TESTS=$((TOTAL - SKIPPED))

echo "================================================================"
echo "IO Benchmark v5.3 — No strace (4c, 2nd run for 100/1000 conn)"
echo "Models: ${MODELS[*]}"
echo "Connections: ${CONNECTIONS[*]}"
echo "Key connections (2 runs): ${KEY_CONNECTIONS[*]}"
echo "Data sizes: ${DATA_SIZES[*]}"
echo "CPU config: $CPU_CONFIG"
echo "strace: DISABLED (all models)"
echo "Total tests: $TOTAL (200 base + 80 key 2nd run)"
echo "Results dir: $RESULTS_BASE_DIR"
if [ "$SKIPPED" -gt 0 ]; then
    echo "Already completed: $SKIPPED (will skip)"
    echo "Remaining: $REMAINING_TESTS"
fi
echo "================================================================"
echo ""

send_telegram "🚀 *Benchmark v5.3 started* (no strace)
Tests: ${TOTAL} (200 + 80 key) | Skipped: ${SKIPPED} | Remaining: ${REMAINING_TESTS}
Models: ${MODELS[*]}
CPU: 4c | 2nd run: 100, 1000 conn"

if [ "$REMAINING_TESTS" -eq 0 ]; then
    echo "All tests already completed. Nothing to do."
    exit 0
fi

# Initial delay — time to disconnect and free resources
echo "Waiting 50 seconds before starting..."
sleep 50

# Kill any leftover Gradle daemons to free memory
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" --stop 2>/dev/null || true
sleep 2

# Build everything first
echo "Building project..."
cd "$BENCHMARK_DIR"
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" build -x test --quiet 2>/dev/null || true
echo ""

for model in "${MODELS[@]}"; do
    for conns in "${CONNECTIONS[@]}"; do
        MAX_RUNS=$(max_runs_for_conn "$conns")
        for size in "${DATA_SIZES[@]}"; do
            for run in $(seq 1 "$MAX_RUNS"); do
                CURRENT=$((CURRENT + 1))

                # Skip already completed tests (resume support)
                RESULTS_DIR="$RESULTS_BASE_DIR/${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run}"
                if [ -f "$RESULTS_DIR/throughput.csv" ] && [ -f "$RESULTS_DIR/latency.csv" ]; then
                    echo "[$CURRENT/$TOTAL] SKIP (already done): ${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run}"
                    continue
                fi

                # Disk space safety check
                AVAIL_GB=$(df --output=avail /ssd 2>/dev/null | tail -1 | awk '{printf "%.0f", $1/1048576}')
                if [ "$AVAIL_GB" -lt 20 ]; then
                    echo "CRITICAL: Only ${AVAIL_GB}GB free on /ssd. Stopping."
                    send_telegram "⛔ *Benchmark v5.3 STOPPED*
Disk space critical: ${AVAIL_GB}GB free
Completed: $((CURRENT - 1))/$TOTAL | Failed: $FAILED"
                    exit 1
                fi

                ELAPSED=$(( $(date +%s) - START_TIME ))
                DONE_NEW=$((CURRENT - SKIPPED))
                if [ "$DONE_NEW" -gt 1 ]; then
                    AVG=$((ELAPSED / (DONE_NEW - 1)))
                    LEFT=$((TOTAL - CURRENT))
                    ETA_MIN=$(( (AVG * LEFT) / 60 ))
                    echo "[$CURRENT/$TOTAL] ETA: ~${ETA_MIN} min | ${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run}"
                else
                    echo "[$CURRENT/$TOTAL] ${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run}"
                fi

                "$SCRIPTS_DIR/run_single_test.sh" \
                    "$model" "$PORT" "$conns" "$size" "$CPU_CONFIG" "$run" || {
                    echo "WARNING: Test ${model}_${CPU_CONFIG}_${conns}conn_${size}_run${run} FAILED"
                    FAILED=$((FAILED + 1))
                    sleep 5
                    continue
                }

                echo ""
            done
        done
    done
done

TOTAL_TIME=$(( $(date +%s) - START_TIME ))
TOTAL_MIN=$((TOTAL_TIME / 60))

# Run syscall profiling pass automatically after main benchmarks
echo ""
echo "================================================================"
echo "Main benchmarks complete. Starting syscall profiling pass..."
echo "================================================================"
echo ""

"$SCRIPTS_DIR/run_syscall_profile.sh" "$PORT" || {
    echo "WARNING: Syscall profiling failed"
    send_telegram "⚠️ *Syscall profiling failed* (main benchmarks OK)"
}

FINAL_TIME=$(( $(date +%s) - START_TIME ))
FINAL_MIN=$((FINAL_TIME / 60))

send_telegram "✅ *Benchmark v5.3 complete!*
Main: ${TOTAL} tests | Skipped: ${SKIPPED} | Failed: ${FAILED}
Syscall: 15 profiles (perf trace)
Time: ${FINAL_MIN} min
Results: $RESULTS_BASE_DIR"

echo "================================================================"
echo "All done! Total time: ${FINAL_MIN} minutes"
echo "Main tests: $TOTAL | Skipped: $SKIPPED | Failed: $FAILED"
echo "Main results: $RESULTS_BASE_DIR"
echo "Syscall profiles: ${BENCHMARK_DIR}/results_v5.3_syscalls"
echo "================================================================"
