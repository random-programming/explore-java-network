#!/usr/bin/env bash
set -euo pipefail

# Run 30 tests to collect correct FD data after fix in collect_metrics.sh
# 6 models × 5 connections × 4c CPU × 4KB data × 1 run = 30 tests

PORT="${1:-8080}"
BENCHMARK_DIR="/ssd/benchmark"
export RESULTS_BASE_DIR="$BENCHMARK_DIR/results_fd_fix"
mkdir -p "$RESULTS_BASE_DIR"

MODELS=(blocking nio epoll iouring iouring-ffm iouring-ffm-mt)
CONNECTIONS=(1 10 100 1000 10000)
CPU_CONFIG="4c"
DATA_SIZE="4096"
RUN="1"

TOTAL=$((${#MODELS[@]} * ${#CONNECTIONS[@]}))
DONE=0
FAILED=0
START_TIME=$(date +%s)

# Telegram
TG_BOT_TOKEN="8772896317:AAGgI1QYLSjkmDfOtMOkB3uNwok09uz4kJQ"
TG_CHAT_ID="731427851"
send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TG_CHAT_ID}" -d parse_mode="Markdown" \
        -d text="$1" >/dev/null 2>&1 || true
}

send_telegram "🚀 *FD-fix benchmark started*
Tests: $TOTAL (6 models × 5 conn × 4c × 4KB × 1 run)
Port: $PORT"

echo "========================================"
echo "FD-fix benchmark: $TOTAL tests"
echo "========================================"

for MODEL in "${MODELS[@]}"; do
    for CONN in "${CONNECTIONS[@]}"; do
        DONE=$((DONE + 1))
        TEST_NAME="${MODEL}_${CPU_CONFIG}_${CONN}conn_${DATA_SIZE}_run${RUN}"
        RESULT_DIR="$RESULTS_BASE_DIR/$TEST_NAME"

        echo ""
        echo "[$DONE/$TOTAL] $TEST_NAME"

        # Skip if already done (resume support)
        if [ -d "$RESULT_DIR" ] && [ -f "$RESULT_DIR/fd_count.csv" ] && [ -f "$RESULT_DIR/throughput.csv" ]; then
            echo "  -> Already exists, skipping"
            continue
        fi

        # Remove partial results
        rm -rf "$RESULT_DIR"

        # Run test
        if ! bash "$BENCHMARK_DIR/scripts/run_single_test.sh" \
            "$MODEL" "$PORT" "$CONN" "$DATA_SIZE" "$CPU_CONFIG" "$RUN" \
            5 30 3 120 2>&1; then
            echo "  -> FAILED"
            FAILED=$((FAILED + 1))
        fi

        # Kill any leftovers
        pkill -f "run --args=$PORT" 2>/dev/null || true
        sleep 1
        pkill -9 -f "run --args=$PORT" 2>/dev/null || true
        sleep 1
    done
done

END_TIME=$(date +%s)
ELAPSED=$(( (END_TIME - START_TIME) / 60 ))

echo ""
echo "========================================"
echo "DONE: $TOTAL tests in ${ELAPSED} min, $FAILED failures"
echo "========================================"

send_telegram "✅ *FD-fix benchmark complete*
Tests: $TOTAL done, $FAILED failed
Time: ${ELAPSED} min"
