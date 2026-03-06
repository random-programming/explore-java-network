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

# Run the full benchmark matrix.
# Usage: run_all_benchmarks.sh [port]

PORT="${1:-8080}"
BENCHMARK_DIR="/ssd/benchmark"
SCRIPTS_DIR="$BENCHMARK_DIR/scripts"

MODELS=(blocking nio epoll iouring)
CONNECTIONS=(1 10 100 1000 10000)
DATA_SIZES=(64 512 4096 16384 65536 131072 524288 1048576)
CPU_CONFIGS=(1c 4c 8c)
RUNS=2

TOTAL=$((${#MODELS[@]} * ${#CONNECTIONS[@]} * ${#DATA_SIZES[@]} * ${#CPU_CONFIGS[@]} * RUNS))
CURRENT=0
SKIPPED=0
START_TIME=$(date +%s)

# Check how many tests already completed (for resume)
for model in "${MODELS[@]}"; do
    for cpu in "${CPU_CONFIGS[@]}"; do
        for conns in "${CONNECTIONS[@]}"; do
            for size in "${DATA_SIZES[@]}"; do
                for run in $(seq 1 $RUNS); do
                    RESULTS_DIR="$BENCHMARK_DIR/results/${model}_${cpu}_${conns}conn_${size}_run${run}"
                    if [ -f "$RESULTS_DIR/throughput.csv" ] && [ -f "$RESULTS_DIR/latency.csv" ]; then
                        SKIPPED=$((SKIPPED + 1))
                    fi
                done
            done
        done
    done
done
REMAINING_TESTS=$((TOTAL - SKIPPED))

echo "================================================================"
echo "IO Benchmark — Full Matrix"
echo "Models: ${MODELS[*]}"
echo "Connections: ${CONNECTIONS[*]}"
echo "Data sizes: ${DATA_SIZES[*]}"
echo "CPU configs: ${CPU_CONFIGS[*]}"
echo "Runs per config: $RUNS"
echo "Total tests: $TOTAL"
if [ "$SKIPPED" -gt 0 ]; then
    echo "Already completed: $SKIPPED (will skip)"
    echo "Remaining: $REMAINING_TESTS"
fi
echo "================================================================"
echo ""

send_telegram "🚀 *Benchmark started*
Tests: ${TOTAL} | Skipped: ${SKIPPED} | Remaining: ${REMAINING_TESTS}
Models: ${MODELS[*]}
Connections: ${CONNECTIONS[*]}
Data sizes: ${DATA_SIZES[*]}
CPU: ${CPU_CONFIGS[*]} | Runs: ${RUNS}"

# Initial delay — time to disconnect and free resources
echo "Waiting 40 seconds before starting..."
sleep 40

# Kill any leftover Gradle daemons to free memory
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" --stop 2>/dev/null || true
sleep 2

# Build everything first
echo "Building project..."
cd "$BENCHMARK_DIR"
"$BENCHMARK_DIR/gradlew" -p "$BENCHMARK_DIR" build -x test --quiet 2>/dev/null || true
echo ""

for model in "${MODELS[@]}"; do
    for cpu in "${CPU_CONFIGS[@]}"; do
        for conns in "${CONNECTIONS[@]}"; do
            for size in "${DATA_SIZES[@]}"; do
                for run in $(seq 1 $RUNS); do
                    CURRENT=$((CURRENT + 1))

                    # Skip already completed tests (resume support)
                    RESULTS_DIR="$BENCHMARK_DIR/results/${model}_${cpu}_${conns}conn_${size}_run${run}"
                    if [ -f "$RESULTS_DIR/throughput.csv" ] && [ -f "$RESULTS_DIR/latency.csv" ]; then
                        echo "[$CURRENT/$TOTAL] SKIP (already done): ${model}_${cpu}_${conns}conn_${size}_run${run}"
                        continue
                    fi

                    # Disk space safety check (between tests, does not affect measurements)
                    AVAIL_GB=$(df --output=avail /ssd 2>/dev/null | tail -1 | awk '{printf "%.0f", $1/1048576}')
                    if [ "$AVAIL_GB" -lt 20 ]; then
                        echo "CRITICAL: Only ${AVAIL_GB}GB free on /ssd. Stopping to prevent system hang."
                        send_telegram "⛔ *Benchmark STOPPED*
Disk space critical: ${AVAIL_GB}GB free on /ssd
Completed: $((CURRENT - 1))/$TOTAL tests"
                        exit 1
                    fi

                    ELAPSED=$(( $(date +%s) - START_TIME ))
                    DONE_NEW=$((CURRENT - SKIPPED))
                    if [ "$DONE_NEW" -gt 1 ]; then
                        AVG=$((ELAPSED / (DONE_NEW - 1)))
                        LEFT=$((TOTAL - CURRENT))
                        ETA_MIN=$(( (AVG * LEFT) / 60 ))
                        echo "[$CURRENT/$TOTAL] ETA: ~${ETA_MIN} min remaining"
                    else
                        echo "[$CURRENT/$TOTAL]"
                    fi

                    "$SCRIPTS_DIR/run_single_test.sh" \
                        "$model" "$PORT" "$conns" "$size" "$cpu" "$run"

                    echo ""
                done
            done
        done
    done
done

TOTAL_TIME=$(( $(date +%s) - START_TIME ))
TOTAL_MIN=$((TOTAL_TIME / 60))
send_telegram "✅ *All benchmarks complete!*
Total tests: ${TOTAL} | Skipped: ${SKIPPED}
Total time: ${TOTAL_MIN} min
Results: /ssd/benchmark/results/"

echo "================================================================"
echo "All benchmarks complete! Total time: ${TOTAL_MIN} minutes"
echo "Results in: $BENCHMARK_DIR/results/"
echo "================================================================"
