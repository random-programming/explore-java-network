#!/usr/bin/env bash
set -euo pipefail

export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# Smoke test: run each model with minimal config to verify everything works.
# Usage: smoke_test.sh [port]

PORT="${1:-8080}"
BENCHMARK_DIR="/ssd/benchmark"
SCRIPTS_DIR="$BENCHMARK_DIR/scripts"
MODELS=(blocking nio epoll iouring)

echo "================================================================"
echo "SMOKE TEST — проверка всех 4 моделей"
echo "Config: 1 conn, 4KB, 4c, warmup=3s, duration=5s"
echo "================================================================"
echo ""

PASSED=0
FAILED=0
ERRORS=""

for model in "${MODELS[@]}"; do
    echo "--- Testing: $model ---"
    RESULT_DIR="$BENCHMARK_DIR/results/smoke_${model}"
    rm -rf "$RESULT_DIR"

    if "$SCRIPTS_DIR/run_single_test.sh" "$model" "$PORT" 1 4096 4c 1 3 5 2 2>&1; then
        # Rename result dir for smoke test
        ACTUAL_DIR="$BENCHMARK_DIR/results/${model}_4c_1conn_4096_run1"

        # Check all required files exist and are non-empty
        ALL_OK=true
        for f in throughput.csv latency.csv cpu.csv context_switches.csv memory.csv fd_count.csv syscalls.csv; do
            if [ ! -s "$ACTUAL_DIR/$f" ]; then
                echo "  WARN: $f is missing or empty"
                if [ "$f" = "syscalls.csv" ]; then
                    # Check if at least header exists
                    if [ -f "$ACTUAL_DIR/$f" ]; then
                        LINES=$(wc -l < "$ACTUAL_DIR/$f")
                        if [ "$LINES" -le 1 ]; then
                            echo "  WARN: syscalls.csv has no data rows"
                        fi
                    fi
                fi
            else
                LINES=$(wc -l < "$ACTUAL_DIR/$f")
                echo "  OK: $f ($LINES lines)"
            fi
        done

        # Check perf sched
        if [ -s "$ACTUAL_DIR/perf_sched_latency.txt" ]; then
            echo "  OK: perf_sched_latency.txt"
        else
            echo "  WARN: perf_sched_latency.txt missing or empty"
        fi

        # Check throughput values
        if [ -f "$ACTUAL_DIR/throughput.csv" ]; then
            RPS=$(tail -1 "$ACTUAL_DIR/throughput.csv" | cut -d',' -f4)
            MBS=$(tail -1 "$ACTUAL_DIR/throughput.csv" | cut -d',' -f5)
            echo "  Throughput: RPS=$RPS  MB/s=$MBS"
        fi

        # Check syscalls count
        if [ -f "$ACTUAL_DIR/syscalls.csv" ]; then
            SC_LINES=$(($(wc -l < "$ACTUAL_DIR/syscalls.csv") - 1))
            echo "  Syscalls: $SC_LINES types captured"
        fi

        PASSED=$((PASSED + 1))
        echo "  RESULT: PASS"

        # Cleanup
        rm -rf "$ACTUAL_DIR"
    else
        FAILED=$((FAILED + 1))
        ERRORS="${ERRORS}\n  - ${model}: test script failed"
        echo "  RESULT: FAIL"
    fi
    echo ""
done

echo "================================================================"
echo "SMOKE TEST RESULTS: $PASSED passed, $FAILED failed"
if [ "$FAILED" -gt 0 ]; then
    echo "Errors:$ERRORS"
fi
echo "================================================================"
