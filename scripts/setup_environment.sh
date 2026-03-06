#!/usr/bin/env bash
set -euo pipefail

echo "=== IO Benchmark: Environment Setup ==="

# JDK 21
if ! java -version 2>&1 | grep -q '"21'; then
    echo "Installing OpenJDK 21..."
    apt-get update -qq
    apt-get install -y -qq openjdk-21-jdk
else
    echo "JDK 21 already installed."
fi

# Build tools
echo "Installing build tools and utilities..."
apt-get install -y -qq \
    strace \
    sysstat \
    linux-tools-common \
    linux-tools-generic \
    linux-tools-"$(uname -r)" 2>/dev/null || true

# liburing for FFM demo
echo "Installing liburing-dev..."
apt-get install -y -qq liburing-dev 2>/dev/null || echo "Warning: liburing-dev not available"

# Python for reports
echo "Installing Python and data science packages..."
apt-get install -y -qq python3 python3-pip python3-venv
pip3 install --quiet jupyter matplotlib pandas seaborn

# Kernel tuning for benchmarks
echo "Applying kernel tuning..."
sysctl -w net.core.somaxconn=65535 2>/dev/null || true
sysctl -w net.ipv4.tcp_max_syn_backlog=65535 2>/dev/null || true
sysctl -w net.ipv4.ip_local_port_range="1024 65535" 2>/dev/null || true
sysctl -w net.ipv4.tcp_tw_reuse=1 2>/dev/null || true
ulimit -n 1048576 2>/dev/null || true

# Build the project
echo "Building project with Gradle..."
cd /ssd/benchmark
./gradlew build -x test --quiet 2>/dev/null || {
    echo "Gradle wrapper not found, trying system gradle..."
    gradle build -x test --quiet
}

echo ""
echo "=== Setup complete ==="
echo "Java: $(java -version 2>&1 | head -1)"
echo "Gradle: ready"
echo "Python: $(python3 --version)"
