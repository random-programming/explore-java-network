#!/usr/bin/env python3
"""Generate benchmark analysis notebook v5.3 — strace отключён для всех моделей, syscall profiling через perf trace, io_uring_enter виден."""
import nbformat as nbf

nb = nbf.v4.new_notebook()
nb.metadata = {
    'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
    'language_info': {'name': 'python', 'version': '3.12.0'}
}

cells = []

# ============================================================
# TITLE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""# Анализ бенчмарка I/O моделей Linux/Java (v5.3)

Сравнение пяти моделей ввода-вывода: **Blocking I/O**, **NIO (Selector)**, **Epoll (native)**, **io_uring (JNI/Netty)**, **io_uring (FFM-MT)**

JDK 21 / Netty 4.1.x / Linux 6.14

**Параметры тестирования:**
- 5 моделей I/O
- 5 уровней параллельных соединений: 1, 10, 100, 1 000, 10 000
- 8 размеров данных: 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB
- CPU-конфигурация: 4 ядра (server: CPU 0,1; client: CPU 2,3)
- 1 run для всех + 2-й run для 100 и 1000 conn
- **Итого: 280 основных тестов + 15 syscall profiles, 0 ошибок, 309 мин**

**Ключевые изменения v5.3 (относительно v5.2):**
- **strace отключён для ВСЕХ моделей.** В v5.2 strace (ptrace) подключался к реальному серверу, деградируя throughput в 3-13x. Теперь throughput и latency отражают реальную производительность.
- **Syscall profiling через `perf trace -s`** (perf_events, не ptrace) — overhead <5%, работает с FFM, видит `io_uring_enter`.
- **15 отдельных syscall-профилей** (5 моделей × 3 conn: 1, 100, 1000) в `results_v5.3_syscalls/`.

**Накопленные исправления (v5–v5.2):**
- PID сервера через `ss -tlnp`, не Gradle wrapper.
- CPU/CS: delta-расчёт, суммирование `/proc/PID/task/*/stat` и `/proc/PID/task/*/status` (все потоки).
- Защита от отрицательных delta.
- FFM-MT: workers = availableProcessors(), SQPOLL off, keep-alive off.
"""
))

# ============================================================
# IMPORTS + CONFIG
# ============================================================
cells.append(nbf.v4.new_code_cell(
r"""import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
import numpy as np
import os, re, subprocess, warnings
from pathlib import Path

warnings.filterwarnings('ignore')
sns.set_theme(style='whitegrid', palette='deep')
plt.rcParams['figure.dpi'] = 110
plt.rcParams['font.size'] = 9

RESULTS_DIR = Path('/ssd/benchmark/results_v5.3')
SYSCALL_DIR = Path('/ssd/benchmark/results_v5.3_syscalls')

MODELS = ['blocking', 'nio', 'epoll', 'iouring', 'iouring-ffm-mt']
MODEL_COLORS = {
    'blocking': '#e74c3c', 'nio': '#3498db', 'epoll': '#2ecc71',
    'iouring': '#9b59b6', 'iouring-ffm-mt': '#1abc9c'
}
MODEL_LABELS = {
    'blocking': 'Blocking I/O', 'nio': 'NIO (Selector)', 'epoll': 'Epoll (native)',
    'iouring': 'io_uring (JNI)', 'iouring-ffm-mt': 'io_uring (FFM-MT)'
}
MODEL_SHORT = {
    'blocking': 'Block', 'nio': 'NIO', 'epoll': 'Epoll',
    'iouring': 'iou', 'iouring-ffm-mt': 'FFM-MT'
}

ALL_SIZES = [64, 512, 4096, 16384, 65536, 131072, 524288, 1048576]
SIZE_LABELS = {64: '64B', 512: '512B', 4096: '4KB', 16384: '16KB',
               65536: '64KB', 131072: '128KB', 524288: '512KB', 1048576: '1MB'}
ALL_CONNS = [1, 10, 100, 1000, 10000]

print("=" * 60)
print("КОНФИГУРАЦИЯ МАШИНЫ")
print("=" * 60)
for cmd, label in [
    ("uname -r", "Kernel"),
    ("lscpu | grep 'Model name' | head -1", "CPU"),
    ("nproc", "CPU cores (total)"),
    ("free -h | grep Mem | awk '{print $2}'", "RAM"),
    ("java -version 2>&1 | head -1", "JDK"),
]:
    try:
        result = subprocess.check_output(cmd, shell=True, text=True).strip()
        print(f"  {label}: {result}")
    except Exception:
        pass
print("=" * 60)
print(f"\nResults dir: {RESULTS_DIR}")
print(f"Models: {MODELS}")
"""
))

# ============================================================
# DATA LOADING
# ============================================================
cells.append(nbf.v4.new_code_cell(
r"""def parse_dir_name(dirname):
    pattern = r'^(blocking|nio|epoll|iouring-ffm-mt|iouring)_(\d+)c_(\d+)conn_(\d+)_run(\d+)$'
    match = re.match(pattern, dirname)
    if not match:
        return None
    return {
        'model': match.group(1), 'cores': int(match.group(2)),
        'connections': int(match.group(3)), 'data_size': int(match.group(4)),
        'run': int(match.group(5))
    }

def load_all_results():
    all_data = {n: [] for n in ['throughput','latency','cpu','context_switches','memory','fd_count','syscalls']}
    strace_raw = {}
    for d in sorted(RESULTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = parse_dir_name(d.name)
        if meta is None:
            continue
        for csv_name, key in [
            ('throughput.csv','throughput'),('latency.csv','latency'),('cpu.csv','cpu'),
            ('context_switches.csv','context_switches'),('memory.csv','memory'),
            ('fd_count.csv','fd_count'),('syscalls.csv','syscalls'),
        ]:
            p = d / csv_name
            if p.exists():
                try:
                    df = pd.read_csv(p)
                    for k, v in meta.items():
                        df[k] = v
                    all_data[key].append(df)
                except Exception:
                    pass
        strace_path = d / 'strace_raw.txt'
        if strace_path.exists():
            try:
                text = strace_path.read_text()
                model = meta['model']
                if model not in strace_raw:
                    strace_raw[model] = []
                strace_raw[model].append((meta, text))
            except Exception:
                pass
    result = {k: pd.concat(v, ignore_index=True) for k, v in all_data.items() if v}
    for key in result:
        for col in result[key].columns:
            if col not in ('model', 'syscall_name'):
                result[key][col] = pd.to_numeric(result[key][col], errors='coerce')
    result['_strace_raw'] = strace_raw
    return result

data = load_all_results()
strace_raw_data = data.pop('_strace_raw', {})
print(f'Loaded: {list(data.keys())}')
for k, v in data.items():
    print(f'  {k}: {len(v)} rows')
print(f'\nModels in data: {sorted(data["throughput"]["model"].unique())}')
print(f'Strace raw files: { {m: len(v) for m, v in strace_raw_data.items()} }')

# Quick validation
tp = data['throughput']
print(f'\nTotal test configs: {tp.groupby(["model","connections","data_size","run"]).ngroups}')
print(f'Connections: {sorted(tp["connections"].unique())}')
print(f'Data sizes: {sorted(tp["data_size"].unique())}')
print(f'Runs: {sorted(tp["run"].unique())}')
"""
))

# ============================================================
# SECTION 1: THROUGHPUT vs CONNECTIONS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 1. Throughput vs Connections

Пропускная способность (запросы/с) в зависимости от числа параллельных соединений.
Каждый subplot — один размер данных. CPU конфигурация: 4 ядра (server: CPU 0,1; client: CPU 2,3).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data:
    df = data['throughput'].copy()
    df_nonzero = df[df['throughput_rps'] > 0]
    agg = df_nonzero.groupby(['model','connections','data_size'])['throughput_rps'].mean().reset_index()

    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Throughput vs Connections — 4 ядра (server: CPU 0,1)', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = agg[agg['data_size'] == size]
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['throughput_rps'], 'o-',
                        color=MODEL_COLORS[model], label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('RPS')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6, loc='best')
    plt.tight_layout()
    plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Throughput

- **Blocking I/O** — наименьшая пропускная способность при высоком числе соединений. Модель «один поток на соединение» создаёт огромные накладные расходы.
- **Epoll (native)** и **io_uring (JNI)** — лидеры среди Netty-моделей. Event-driven с мультиплексированием.
- **NIO (Selector)** — промежуточная позиция: event-driven через `java.nio.channels.Selector`.
- **FFM-MT** — многопоточная архитектура (acceptor + N workers), каждый со своим io_uring ring.
- При больших payload (512KB-1MB) все модели сходятся — bottleneck в передаче данных.
"""
))

# ============================================================
# SECTION 2: THROUGHPUT vs DATA SIZE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 2. Throughput vs Data Size

Влияние размера payload на пропускную способность при фиксированном числе соединений.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data:
    df = data['throughput'].copy()
    df_nonzero = df[df['throughput_rps'] > 0]
    agg = df_nonzero.groupby(['model','connections','data_size'])['throughput_rps'].mean().reset_index()

    fig, axes = plt.subplots(1, 5, figsize=(24, 5))
    fig.suptitle('Throughput vs Payload Size — 4 ядра', fontsize=14, fontweight='bold')
    for idx, conns in enumerate(ALL_CONNS):
        ax = axes[idx]
        subset = agg[agg['connections'] == conns]
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('data_size')
            if not m.empty:
                ax.plot([SIZE_LABELS[s] for s in m['data_size']], m['throughput_rps'], 'o-',
                        color=MODEL_COLORS[model], label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_title(f'{conns} conn', fontsize=11, fontweight='bold')
        ax.set_xlabel('Payload')
        ax.set_ylabel('RPS')
        ax.tick_params(axis='x', rotation=45)
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()
"""
))

# ============================================================
# SECTION 3: TIME-SERIES
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 3. Time-Series: метрики по секундам

Динамика метрик в течение 30-секундного теста. Конфигурации: 1000 conn (типичная нагрузка) и 100 conn (для контраста).

**v5.3:** strace отключён → throughput и latency без искажений. CPU и context switches суммируются по всем потокам (`/proc/PID/task/*/stat`, `/proc/PID/task/*/status`). Защита от отрицательных delta.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""def plot_timeseries_grid(data, conns=1000, size=4096, run=1):
    title_suffix = f'4 cores, {conns} conns, {SIZE_LABELS[size]}, run {run}'
    metrics = [
        ('throughput', 'throughput_rps', 'RPS'),
        ('latency', 'p50_us', 'Latency p50 (us)'),
        ('latency', 'p99_us', 'Latency p99 (us)'),
        ('cpu', 'server_total_pct', 'Server CPU % (delta)'),
        ('context_switches', 'server_voluntary', 'Vol CS / sec (delta)'),
        ('memory', 'server_rss_kb', 'RSS (KB)'),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(20, 8))
    fig.suptitle(f'Time-Series — {title_suffix}', fontsize=13, fontweight='bold')
    for idx, (ds, col, ylabel) in enumerate(metrics):
        ax = axes[idx // 3][idx % 3]
        if ds not in data:
            continue
        df = data[ds]
        mask = (df['connections'] == conns) & (df['data_size'] == size) & (df['run'] == run)
        subset = df[mask]
        if subset.empty or col not in subset.columns:
            continue
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('timestamp_sec')
            if not m.empty:
                ax.plot(m['timestamp_sec'], m[col], '-', color=MODEL_COLORS[model],
                        label=MODEL_LABELS[model], linewidth=1.2, alpha=0.85)
        ax.set_xlabel('Sec')
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()

if data:
    plot_timeseries_grid(data, conns=1000, size=4096, run=1)
    plot_timeseries_grid(data, conns=100, size=4096, run=1)
    plot_timeseries_grid(data, conns=1000, size=1048576, run=1)
    plot_timeseries_grid(data, conns=10000, size=4096, run=1)
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы: динамика во времени

- **CPU (delta, все потоки):** теперь видна реальная нагрузка сервера. Event-driven модели показывают различную утилизацию CPU.
- **Context switches (delta/sec, все потоки):** Blocking 89K-209K CS/sec, Netty 84K-178K, FFM-MT **16K-38K** (в 5-9x меньше). Данные теперь ненулевые благодаря итерации по `/proc/PID/task/*/status`.
- **Throughput** стабилизируется после 3-5 секунд (JVM warm-up + JIT).
- **Memory (RSS)** — реальное потребление сервера (370-530 MB), не Gradle wrapper (~100 MB).
"""
))

# ============================================================
# SECTION 4: LATENCY DISTRIBUTION
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 4. Latency Distribution

Box-plot распределения задержек (p99) по моделям для всех размеров данных.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'latency' in data:
    df = data['latency']

    for conns in [100, 1000, 10000]:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'Latency p99 — 4 ядра, {conns} conn', fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = df[(df['connections'] == conns) & (df['data_size'] == size)]
            if subset.empty:
                ax.set_title(SIZE_LABELS[size])
                continue
            plot_data, labels, colors = [], [], []
            for model in MODELS:
                m = subset[subset['model'] == model]['p99_us']
                if not m.empty:
                    plot_data.append(m.values)
                    labels.append(MODEL_SHORT[model])
                    colors.append(MODEL_COLORS[model])
            if plot_data:
                bp = ax.boxplot(plot_data, labels=labels, patch_artist=True, widths=0.6)
                for patch, color in zip(bp['boxes'], colors):
                    patch.set_facecolor(color)
                    patch.set_alpha(0.7)
            ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
            ax.set_ylabel('p99 (us)')
            ax.tick_params(axis='x', rotation=30, labelsize=7)
            ax.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Latency

- **Blocking I/O** при 1000+ conn: p99 latency взлетает до секунд. Потоки ждут планировщика ОС.
- **Event-driven модели (NIO, Epoll, io_uring JNI):** p99 на порядки ниже — небольшое число потоков обслуживает все соединения.
- **FFM-MT:** latency сопоставим с Netty-моделями при эквивалентном числе соединений.
"""
))

# ============================================================
# SECTION 5: CPU UTILIZATION
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 5. CPU Utilization (исправленные метрики)

Использование CPU сервером: user-space + kernel-space. **Delta-значения** (мгновенная нагрузка за секунду), нормализованные на число ядер сервера (2).

**Методика сбора (v5–v5.3):**
- CPU суммируется из `/proc/PID/task/*/stat` (все потоки процесса)
- Защита от отрицательных delta (`max(0, delta)`)
- PID реального Java-сервера (не Gradle wrapper)
- Delta расчёт вместо кумулятивного
- Нормализация на 2 ядра сервера (не 16 ядер машины)
- strace отключён — нет ptrace overhead
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'cpu' in data:
    df = data['cpu']
    agg = df.groupby(['model','connections','data_size']).agg(
        user=('server_user_pct','mean'), sys=('server_sys_pct','mean'),
        total=('server_total_pct','mean')
    ).reset_index()

    conn_colors = {1: '#2196F3', 10: '#00BCD4', 100: '#4CAF50', 1000: '#FF9800', 10000: '#E91E63'}

    fig, axes = plt.subplots(2, 4, figsize=(24, 10))
    fig.suptitle('Server CPU — 4 ядра (server: CPU 0,1)\n'
                 '(сплошная = user, прозрачная верхушка = sys)',
                 fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = agg[agg['data_size'] == size]
        if subset.empty:
            ax.set_title(SIZE_LABELS[size])
            continue
        n_models = len(MODELS)
        n_conns = len(ALL_CONNS)
        bar_width = 0.8 / n_conns
        x = np.arange(n_models)

        for i, conns in enumerate(ALL_CONNS):
            user_vals, sys_vals = [], []
            for model in MODELS:
                row = subset[(subset['model'] == model) & (subset['connections'] == conns)]
                user_vals.append(row['user'].values[0] if not row.empty else 0)
                sys_vals.append(row['sys'].values[0] if not row.empty else 0)
            offset = (i - (n_conns - 1) / 2) * bar_width
            color = conn_colors[conns]
            ax.bar(x + offset, user_vals, bar_width * 0.9, color=color, alpha=0.85,
                   label=f'{conns} conn' if idx == 0 else '')
            ax.bar(x + offset, sys_vals, bar_width * 0.9, bottom=user_vals,
                   color=color, alpha=0.3)

        ax.set_xticks(x)
        ax.set_xticklabels([MODEL_SHORT[m] for m in MODELS], fontsize=7, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('CPU %')
        ax.grid(True, alpha=0.3, axis='y')
    axes[0][0].legend(fontsize=7, loc='best', title='Connections', title_fontsize=7)
    plt.tight_layout()
    plt.show()

    # Summary table
    print("\n" + "=" * 70)
    print("  СВОДКА: средний CPU % по моделям и conn (4KB)")
    print("=" * 70)
    pivot_data = agg[agg['data_size'] == 4096]
    if not pivot_data.empty:
        pivot = pivot_data.pivot_table(
            values='total', index='model', columns='connections', aggfunc='mean'
        ).round(1)
        pivot.index = [MODEL_LABELS.get(m, m) for m in pivot.index]
        display(pivot)
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по CPU

- **Blocking I/O:** высокий kernel CPU при большом числе соединений — планирование тысяч потоков.
- **Event-driven (NIO, Epoll, io_uring JNI):** меньше kernel CPU — один `epoll_wait()`/`io_uring_enter()` обрабатывает batch событий.
- **FFM-MT:** CPU профиль теперь корректно отображает нагрузку сервера.
- При малых payload (64B-4KB) CPU-bound, при больших — bandwidth-bound.
"""
))

# ============================================================
# SECTION 6: CONTEXT SWITCHES
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 6. Context Switches (исправленные метрики)

Добровольные переключения контекста сервера **в секунду** (delta). Ключевой индикатор архитектурной разницы.

**Методика (v5.2+):** CS суммируются из `/proc/PID/task/*/status` (все потоки). strace отключён — нет ptrace overhead.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'context_switches' in data:
    df = data['context_switches']
    agg = df.groupby(['model','connections','data_size']).agg(
        vol=('server_voluntary','mean'), invol=('server_involuntary','mean')
    ).reset_index()

    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Voluntary Context Switches / sec — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = agg[agg['data_size'] == size]
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['vol'], 'o-', color=MODEL_COLORS[model],
                        label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('Vol CS / sec')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()

    # Involuntary CS
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Involuntary Context Switches / sec — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = agg[agg['data_size'] == size]
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['invol'], 'o-', color=MODEL_COLORS[model],
                        label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('Invol CS / sec')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Context Switches

- **FFM-MT:** 16K-38K voluntary CS/sec — **в 5-9x меньше**, чем у Netty-моделей и Blocking. io_uring batching (submit + wait в одном io_uring_enter) минимизирует переключения.
- **Blocking I/O:** 89K-209K CS/sec — максимум среди всех моделей. Thread-per-connection создаёт массивный scheduling overhead.
- **Netty (NIO, Epoll, io_uring JNI):** 83K-178K CS/sec — промежуточные значения. Event loop + epoll_wait/io_uring_enter генерирует меньше CS, чем blocking, но больше, чем FFM-MT.
- **Involuntary CS:** FFM-MT показывает повышенные involuntary CS (до 1200/sec) — конкуренция worker'ов за 2 ядра CPU.
"""
))

# ============================================================
# SECTION 6b: ANOMALY — io_uring JNI cold start
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 6b. Аномалия: io_uring JNI при 100 conn (cold start)

Обнаружена воспроизводимая нестабильность при запуске io_uring JNI с 100 параллельными соединениями:

| Прогон | Средний RPS | Максимум RPS | Минимум RPS |
|--------|------------|-------------|-------------|
| **run1** | **8,852** | 28,007 | 6,211 |
| **run2** | **39,425** | 113,518 | 6,605 |

Разница **4.5x** между run1 и run2. Run1 запускался первым (cold start), run2 — сразу после.

**Гипотезы:**
1. **JVM cold start:** JIT не прогрет, C2 компиляция не завершена за 5 сек warmup
2. **Gradle daemon state:** JVM от предыдущего теста влияет на Gradle daemon
3. **Предыдущий тест не полностью cleanup:** порт в TIME_WAIT, io_uring ring state
4. **Netty io_uring инициализация:** первый запуск native transport загружает .so и инициализирует кольца

Аномалия специфична для io_uring JNI — другие модели не показывают такого разброса между run1 и run2.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# Визуализация аномалии: io_uring JNI run1 vs run2 при 100 conn, 4KB
if 'throughput' in data:
    df = data['throughput']
    fig, axes = plt.subplots(1, 2, figsize=(16, 5))
    fig.suptitle('Аномалия io_uring JNI: 100 conn, 4KB — run1 vs run2', fontsize=14, fontweight='bold')

    for run_idx, run in enumerate([1, 2]):
        ax = axes[run_idx]
        mask = (df['model'] == 'iouring') & (df['connections'] == 100) & (df['data_size'] == 4096) & (df['run'] == run)
        subset = df[mask].sort_values('timestamp_sec')
        if not subset.empty:
            ax.plot(subset['timestamp_sec'], subset['throughput_rps'], '-o',
                    color=MODEL_COLORS['iouring'], linewidth=1.5, markersize=3)
            avg_rps = subset['throughput_rps'].mean()
            ax.axhline(y=avg_rps, color='red', linestyle='--', alpha=0.7, label=f'avg={avg_rps:,.0f}')
            ax.set_title(f'Run {run} (avg={avg_rps:,.0f} RPS)', fontsize=12, fontweight='bold')
        ax.set_xlabel('Sec')
        ax.set_ylabel('RPS')
        ax.grid(True, alpha=0.3)
        ax.legend()
    plt.tight_layout()
    plt.show()

    # Сравнение run1 vs run2 для всех моделей при 100 conn, 4KB
    print("\n" + "=" * 70)
    print("  run1 vs run2: 100 conn, 4KB")
    print("=" * 70)
    for model in MODELS:
        for run in [1, 2]:
            mask = (df['model'] == model) & (df['connections'] == 100) & (df['data_size'] == 4096) & (df['run'] == run)
            subset = df[mask]
            if not subset.empty:
                avg = subset['throughput_rps'].mean()
                print(f"  {MODEL_LABELS[model]:25s} run{run}: {avg:>10,.0f} RPS")
"""
))

# ============================================================
# SECTION 7: SYSCALL PROFILING (perf trace)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 7. Syscall Profiling (perf trace)

Профилирование системных вызовов через `perf trace -s -p PID` (perf_events, kernel tracing).

**Почему perf trace, а не strace:**

| Характеристика | strace | perf trace |
|---------------|--------|------------|
| Механизм | ptrace (interrupt на каждый syscall) | perf_events (kernel tracing) |
| Overhead | **3-13x деградация throughput** | **< 5%** |
| Совместимость с FFM | Нет (крашит io_uring через FFM) | Да |
| Видит io_uring_enter | Только через FFM | **Да, для всех** |

В v5.2 strace подключался к реальному серверу (PID через `ss -tlnp`), что деградировало throughput в 3-13x. В v5.3 strace **полностью отключён** для основных 280 тестов. Syscall profile собирается отдельно через perf trace (15 тестов в `results_v5.3_syscalls/`).

> **Ключевое:** `io_uring_enter` теперь виден у FFM-MT — acceptor выполняет >2M вызовов за 10 секунд.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""import re as _re

def parse_perf_trace(filepath):
    # Parse perf trace -s output: extract per-thread syscall summaries.
    threads = {}
    current_thread = None
    with open(filepath) as f:
        for line in f:
            # Thread header: " thread_name (PID), N events, X%"
            tm = _re.match(r'\s+(.+?)\s+\((\d+)\),\s+(\d+)\s+events', line)
            if tm:
                current_thread = tm.group(1).strip()
                threads[current_thread] = {}
                continue
            # Syscall line: "   syscall_name   calls  errors  total ..."
            sm = _re.match(r'\s{3}(\w+)\s+(\d+)\s+(\d+)\s+([\d.]+)', line)
            if sm and current_thread:
                syscall = sm.group(1)
                calls = int(sm.group(2))
                threads[current_thread][syscall] = calls
    return threads

def aggregate_perf_trace(threads):
    # Aggregate syscall counts across all threads.
    total = {}
    for thread_syscalls in threads.values():
        for sc, count in thread_syscalls.items():
            total[sc] = total.get(sc, 0) + count
    return total

# Load perf trace data
perf_trace_data = {}
if SYSCALL_DIR.exists():
    for d in sorted(SYSCALL_DIR.iterdir()):
        if not d.is_dir():
            continue
        pt_file = d / 'perf_trace.txt'
        if not pt_file.exists():
            continue
        # Parse dir name: model_4c_Nconn_4096
        pm = _re.match(r'^(blocking|nio|epoll|iouring-ffm-mt|iouring)_(\d+)c_(\d+)conn_(\d+)$', d.name)
        if not pm:
            continue
        model = pm.group(1)
        conns = int(pm.group(3))
        threads = parse_perf_trace(str(pt_file))
        if not threads:
            continue
        agg = aggregate_perf_trace(threads)
        if model not in perf_trace_data:
            perf_trace_data[model] = {}
        perf_trace_data[model][conns] = {'threads': threads, 'aggregate': agg}

print(f"Loaded perf trace profiles: {len(perf_trace_data)} models")
for model, conns_data in sorted(perf_trace_data.items()):
    print(f"  {MODEL_LABELS.get(model, model)}: {sorted(conns_data.keys())} conn")
"""
))

# Perf trace detailed analysis
cells.append(nbf.v4.new_markdown_cell(
"""### 7.1 Детальный профиль syscalls по моделям (perf trace)

Агрегированный профиль системных вызовов: суммарные вызовы по всем потокам за 10 секунд.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if perf_trace_data:
    for conns in [1, 100, 1000]:
        print(f"\n{'='*80}")
        print(f"  {conns} connections — Top syscalls (perf trace, 10 sec)")
        print(f"{'='*80}")
        for model in MODELS:
            model_data = perf_trace_data.get(model, {})
            if conns not in model_data:
                print(f"  {MODEL_LABELS[model]:25s}: нет данных")
                continue
            agg = model_data[conns]['aggregate']
            total_calls = sum(agg.values())
            sorted_sc = sorted(agg.items(), key=lambda x: -x[1])[:8]
            print(f"\n  {MODEL_LABELS[model]:25s} (total: {total_calls:,} calls)")
            for sc_name, sc_count in sorted_sc:
                pct = sc_count * 100 / total_calls if total_calls > 0 else 0
                print(f"    {sc_name:25s}: {sc_count:>10,}  ({pct:5.1f}%)")

    # Bar chart: top syscalls comparison at 100 conn
    fig, axes = plt.subplots(1, len(MODELS), figsize=(24, 6))
    fig.suptitle('Top Syscalls по моделям (4c, 100 conn, 4KB) — perf trace', fontsize=14, fontweight='bold')
    for i, model in enumerate(MODELS):
        ax = axes[i]
        model_data = perf_trace_data.get(model, {})
        if 100 in model_data:
            agg = model_data[100]['aggregate']
            sorted_sc = sorted(agg.items(), key=lambda x: -x[1])[:10]
            if sorted_sc:
                names = [s[0] for s in sorted_sc]
                counts = [s[1] for s in sorted_sc]
                ax.barh(range(len(names)), counts, color=MODEL_COLORS[model], alpha=0.8)
                ax.set_yticks(range(len(names)))
                ax.set_yticklabels(names, fontsize=7)
                ax.set_xlabel('Count')
                ax.invert_yaxis()
                ax.grid(True, alpha=0.3)
        ax.set_title(MODEL_SHORT[model], fontsize=11, fontweight='bold', color=MODEL_COLORS[model])
    plt.tight_layout()
    plt.show()

    # Total syscalls comparison bar chart
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle('Общее количество syscalls: модели × уровни нагрузки', fontsize=14, fontweight='bold')
    for idx, conns in enumerate([1, 100, 1000]):
        ax = axes[idx]
        vals, colors, labels = [], [], []
        for model in MODELS:
            model_data = perf_trace_data.get(model, {})
            total = sum(model_data[conns]['aggregate'].values()) if conns in model_data else 0
            vals.append(total)
            colors.append(MODEL_COLORS[model])
            labels.append(MODEL_SHORT[model])
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=9)
        ax.set_title(f'{conns} conn', fontsize=12, fontweight='bold')
        ax.set_ylabel('Total syscalls (10 sec)')
        ax.grid(True, alpha=0.3, axis='y')
        for bar, val in zip(bars, vals):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width()/2., bar.get_height(),
                        f'{val:,.0f}', ha='center', va='bottom', fontsize=7)
    plt.tight_layout()
    plt.show()
"""
))

# io_uring_enter visibility
cells.append(nbf.v4.new_markdown_cell(
"""### 7.2 io_uring_enter: видимость через perf trace

Ключевое: `perf trace` (perf_events) видит `io_uring_enter` — syscall, невидимый для strace при работе через JNI.

FFM-MT acceptor (main thread) вызывает `io_uring_enter` >2M раз за 10 секунд. Worker'ы — по ~70K раз. Это подтверждает, что io_uring ring реально используется.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if perf_trace_data and 'iouring-ffm-mt' in perf_trace_data:
    print("=" * 80)
    print("  io_uring FFM-MT: потоковый профиль syscalls (perf trace)")
    print("=" * 80)

    for conns in [1, 100, 1000]:
        model_data = perf_trace_data.get('iouring-ffm-mt', {})
        if conns not in model_data:
            continue
        threads = model_data[conns]['threads']
        print(f"\n--- {conns} connections ---")
        for thread_name, syscalls in sorted(threads.items(), key=lambda x: -sum(x[1].values())):
            total = sum(syscalls.values())
            if total < 10:
                continue
            top_sc = sorted(syscalls.items(), key=lambda x: -x[1])[:5]
            top_str = ', '.join(f'{s}:{c:,}' for s, c in top_sc)
            print(f"  {thread_name:25s}: {total:>10,} calls  |  {top_str}")

    # Comparison: io_uring_enter calls across models
    print(f"\n{'='*80}")
    print("  io_uring_enter: сравнение моделей (100 conn)")
    print(f"{'='*80}")
    for model in MODELS:
        model_data = perf_trace_data.get(model, {})
        if 100 not in model_data:
            print(f"  {MODEL_LABELS[model]:25s}: нет данных")
            continue
        agg = model_data[100]['aggregate']
        iouring_calls = agg.get('io_uring_enter', 0)
        total_calls = sum(agg.values())
        pct = iouring_calls * 100 / total_calls if total_calls > 0 else 0
        print(f"  {MODEL_LABELS[model]:25s}: io_uring_enter = {iouring_calls:>10,}  ({pct:.1f}% от всех syscalls)")
"""
))

# ============================================================
# SECTION 8: MEMORY USAGE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 8. Memory Usage

RSS (Resident Set Size) сервера — фактически занятая физическая память.

**Исправление v5:** RSS реального Java-сервера (370-530 MB), а не Gradle wrapper (~100 MB).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'memory' in data:
    df = data['memory'].copy()
    df = df[df['server_rss_kb'] > 0]
    agg = df.groupby(['model','connections','data_size'])['server_rss_kb'].mean().reset_index()
    agg['rss_mb'] = agg['server_rss_kb'] / 1024

    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Server Memory (RSS) — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = agg[agg['data_size'] == size]
        for model in MODELS:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['rss_mb'], 'o-', color=MODEL_COLORS[model],
                        label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('RSS (MB)')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()

    # Summary table
    print("\n" + "=" * 70)
    print("  СВОДКА: средний RSS (MB) по моделям и conn (4KB)")
    print("=" * 70)
    pivot = agg[agg['data_size'] == 4096].pivot_table(
        values='rss_mb', index='model', columns='connections', aggfunc='mean'
    ).round(1)
    pivot.index = [MODEL_LABELS.get(m, m) for m in pivot.index]
    display(pivot)
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Memory

- **Blocking I/O:** при 10000 conn потенциально создаёт тысячи потоков (1 MB стека каждый).
- **Netty-модели (NIO, Epoll, io_uring JNI):** PooledByteBufAllocator — per-core arenas.
- **FFM-MT:** fixed buffers (2048 x 4KB per worker) + Arena.ofConfined() per-worker. Нет Netty PooledByteBufAllocator.
- RSS теперь отражает реальное потребление Java-сервера (~370-530 MB), а не Gradle wrapper (~100 MB).
"""
))

# ============================================================
# SECTION 9: FILE DESCRIPTORS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 9. File Descriptors

Серверные файловые дескрипторы. PID сервера определяется через `ss -tlnp` по порту.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'fd_count' in data:
    df = data['fd_count']
    agg = df.groupby(['model','connections','data_size'])['server_fd_count'].max().reset_index()

    fig, ax = plt.subplots(1, 1, figsize=(14, 7))
    fig.suptitle('File Descriptors — 4 ядра, 4KB', fontsize=14, fontweight='bold')
    subset = agg[agg['data_size'] == 4096]
    for model in MODELS:
        m = subset[subset['model'] == model].sort_values('connections')
        if not m.empty:
            ax.plot(m['connections'], m['server_fd_count'], 'o-', color=MODEL_COLORS[model],
                    label=MODEL_LABELS[model], linewidth=2, markersize=6)
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('Connections', fontsize=12)
    ax.set_ylabel('Max Server FDs', fontsize=12)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10)
    plt.tight_layout()
    plt.show()

    # Summary table
    print("\n" + "=" * 70)
    print("  СВОДКА: Max Server FD (4KB)")
    print("=" * 70)
    pivot = agg[agg['data_size'] == 4096].pivot_table(
        values='server_fd_count', index='model', columns='connections', aggfunc='max'
    ).round(0).astype(int)
    pivot.index = [MODEL_LABELS.get(m, m) for m in pivot.index]
    display(pivot)
"""
))

# ============================================================
# SECTION 10: SUMMARY TABLES
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 10. Summary Tables

Сводные таблицы ключевых метрик: throughput, latency, CPU, memory для каждого размера данных.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data and 'latency' in data and 'cpu' in data and 'memory' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    tp = tp_nonzero.groupby(['model','connections','data_size'])['throughput_rps'].mean().reset_index()
    lat = data['latency'].groupby(['model','connections','data_size'])[['p50_us','p99_us']].mean().reset_index()
    cpu = data['cpu'].groupby(['model','connections','data_size'])['server_total_pct'].mean().reset_index()
    mem = data['memory'].copy()
    mem = mem[mem['server_rss_kb'] > 0]
    mem = mem.groupby(['model','connections','data_size'])['server_rss_kb'].mean().reset_index()
    mem['rss_mb'] = mem['server_rss_kb'] / 1024

    merged = tp.merge(lat, on=['model','connections','data_size'], how='inner')
    merged = merged.merge(cpu, on=['model','connections','data_size'], how='left')
    merged = merged.merge(mem[['model','connections','data_size','rss_mb']], on=['model','connections','data_size'], how='left')

    for size in [4096, 65536, 1048576]:
        summary = merged[merged['data_size'] == size].copy()
        if summary.empty:
            continue
        summary = summary[['model','connections','throughput_rps','p50_us','p99_us','server_total_pct','rss_mb']].copy()
        summary.columns = ['Model','Conn','RPS','p50 (us)','p99 (us)','CPU %','RSS (MB)']
        summary['RPS'] = summary['RPS'].round(0).astype(int)
        summary['p50 (us)'] = summary['p50 (us)'].round(0).astype(int)
        summary['p99 (us)'] = summary['p99 (us)'].round(0).astype(int)
        summary['CPU %'] = summary['CPU %'].round(1)
        summary['RSS (MB)'] = summary['RSS (MB)'].round(1)
        summary['Model'] = summary['Model'].map(MODEL_LABELS)

        print(f"\n{'='*80}")
        print(f"  Payload: {SIZE_LABELS[size]} — 4 ядра")
        print(f"{'='*80}")
        display(summary.sort_values(['Conn','Model']).reset_index(drop=True))
"""
))

# ============================================================
# SECTION 11: FFM-MT vs JNI DEEP DIVE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 11. FFM-MT vs io_uring JNI: Deep Dive

Ключевое сравнение: Panama FFM vs JNI при эквивалентной многопоточной архитектуре.

**Архитектурное сравнение:**

| Аспект | io_uring JNI (Netty) | io_uring FFM-MT |
|--------|---------------------|-----------------|
| Binding | JNI (C library) | Panama FFM (JDK 21) |
| Threading | IOUringEventLoopGroup(N) | Acceptor + N WorkerThreads |
| Ring per thread | Да | Да (4096 entries) |
| Fixed buffers | Нет | Да (2048 x 4KB per worker) |
| SQPOLL | Нет | **Нет** (отключён в v5) |
| Event loop | Netty EventLoop | Custom Java event loop |
| Keep-alive | Нет (Connection: close) | **Нет** (отключён в v5) |
| Workers | availableProcessors() | **availableProcessors()** (исправлено в v5, было /2) |
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data and 'latency' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    tp_agg = tp_nonzero.groupby(['model','connections','data_size'])['throughput_rps'].mean().reset_index()
    lat_agg = data['latency'].groupby(['model','connections','data_size'])[['p50_us','p99_us']].mean().reset_index()

    compare_models = ['iouring', 'iouring-ffm-mt']

    # Throughput comparison
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('FFM-MT vs JNI: Throughput — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = tp_agg[tp_agg['data_size'] == size]
        for model in compare_models:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['throughput_rps'], 'o-',
                        color=MODEL_COLORS[model], label=MODEL_LABELS[model],
                        linewidth=2, markersize=5)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('RPS')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=8)
    plt.tight_layout()
    plt.show()

    # Latency p99 comparison
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('FFM-MT vs JNI: Latency p99 — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = lat_agg[lat_agg['data_size'] == size]
        for model in compare_models:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['p99_us'], 'o-',
                        color=MODEL_COLORS[model], label=MODEL_LABELS[model],
                        linewidth=2, markersize=5)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('p99 (us)')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=8)
    plt.tight_layout()
    plt.show()

    # Ratio chart: FFM-MT / JNI
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('FFM-MT / JNI Throughput Ratio — 4 ядра (1.0 = равенство)', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        jni_data = tp_agg[(tp_agg['data_size'] == size) &
                          (tp_agg['model'] == 'iouring')].set_index('connections')['throughput_rps']
        ffm_mt_data = tp_agg[(tp_agg['data_size'] == size) &
                             (tp_agg['model'] == 'iouring-ffm-mt')].set_index('connections')['throughput_rps']
        common_conns = sorted(set(jni_data.index) & set(ffm_mt_data.index))
        if common_conns:
            ratios = [ffm_mt_data[c] / jni_data[c] if jni_data[c] > 0 else 0 for c in common_conns]
            bars = ax.bar(range(len(common_conns)), ratios, color='#1abc9c', alpha=0.8)
            ax.set_xticks(range(len(common_conns)))
            ax.set_xticklabels([str(c) for c in common_conns], fontsize=8, rotation=45)
            ax.axhline(y=1.0, color='red', linestyle='--', linewidth=1, alpha=0.7)
            ax.set_xlabel('Connections')
            ax.set_ylabel('FFM-MT / JNI ratio')
            for i, (c, r) in enumerate(zip(common_conns, ratios)):
                ax.text(i, r, f'{r:.2f}', ha='center', va='bottom', fontsize=7)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()
    plt.show()

    # Summary
    print("\n" + "=" * 80)
    print("  FFM-MT vs JNI: сводная таблица (4 ядра)")
    print("=" * 80)
    merged = tp_agg.merge(lat_agg, on=['model','connections','data_size'], how='inner')
    for size in [4096, 65536, 1048576]:
        sub = merged[(merged['data_size'] == size) & (merged['model'].isin(compare_models))]
        if sub.empty:
            continue
        sub = sub[['model','connections','throughput_rps','p50_us','p99_us']].copy()
        sub.columns = ['Model','Conn','RPS','p50 (us)','p99 (us)']
        sub['RPS'] = sub['RPS'].round(0).astype(int)
        sub['p50 (us)'] = sub['p50 (us)'].round(0).astype(int)
        sub['p99 (us)'] = sub['p99 (us)'].round(0).astype(int)
        sub['Model'] = sub['Model'].map(MODEL_LABELS)
        print(f"\n  --- {SIZE_LABELS[size]} ---")
        display(sub.sort_values(['Conn','Model']).reset_index(drop=True))
"""
))

# ============================================================
# SECTION 12: ALL 5 MODELS — BAR CHARTS at 1000 conn
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 12. Сравнение всех 5 моделей при 1000 connections

Барчарт-сравнение throughput, latency, CPU и memory для всех 5 моделей при типичной нагрузке.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data and 'latency' in data and 'cpu' in data and 'memory' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    mem_df = data['memory'].copy()
    mem_df = mem_df[mem_df['server_rss_kb'] > 0]

    conns = 1000

    tp_agg = tp_nonzero[tp_nonzero['connections'] == conns].groupby(
        ['model','data_size'])['throughput_rps'].mean().reset_index()
    lat_agg = data['latency'][data['latency']['connections'] == conns].groupby(
        ['model','data_size'])[['p50_us','p99_us']].mean().reset_index()
    cpu_agg = data['cpu'][data['cpu']['connections'] == conns].groupby(
        ['model','data_size'])['server_total_pct'].mean().reset_index()
    mem_agg = mem_df[mem_df['connections'] == conns].groupby(
        ['model','data_size'])['server_rss_kb'].mean().reset_index()
    mem_agg['rss_mb'] = mem_agg['server_rss_kb'] / 1024

    chart_configs = [
        (tp_agg, 'throughput_rps', 'RPS', f'Throughput: 5 моделей — {conns} conn'),
        (lat_agg, 'p99_us', 'p99 (us)', f'Latency p99: 5 моделей — {conns} conn'),
        (cpu_agg, 'server_total_pct', 'CPU %', f'Server CPU: 5 моделей — {conns} conn'),
        (mem_agg, 'rss_mb', 'RSS (MB)', f'Memory: 5 моделей — {conns} conn'),
    ]

    for agg_df, col, ylabel, title in chart_configs:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(title, fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg_df[agg_df['data_size'] == size]
            vals = []
            for m in MODELS:
                row = subset[subset['model'] == m]
                vals.append(row[col].values[0] if not row.empty else 0)
            colors = [MODEL_COLORS[m] for m in MODELS]
            labels = [MODEL_SHORT[m] for m in MODELS]
            bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, fontsize=8, rotation=30)
            ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3, axis='y')
            for bar, val in zip(bars, vals):
                if val > 0:
                    ax.text(bar.get_x() + bar.get_width()/2., bar.get_height(),
                            f'{val:,.0f}', ha='center', va='bottom', fontsize=6)
        plt.tight_layout()
        plt.show()
"""
))

# ============================================================
# FINAL CONCLUSIONS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 13. Итоговые выводы

### Рейтинг моделей I/O (по совокупности метрик, v5.3)

| Место | Модель | Сильные стороны | Слабые стороны |
|-------|--------|----------------|----------------|
| 1 | **Epoll (native)** | Максимальный RPS среди Netty, низкий tail latency | Требует Linux, Netty native |
| 2 | **io_uring (JNI)** | Сопоставим с Epoll, batch I/O, io_uring API | Linux 5.1+, cold start нестабильность |
| 3 | **io_uring (FFM-MT)** | **5-9x меньше CS**, fixed buffers, `io_uring_enter` виден | JDK 21+, custom event loop |
| 4 | **NIO (Selector)** | Кроссплатформенный, стандартный JDK | Overhead JVM Selector |
| 5 | **Blocking I/O** | Простота кода | Катастрофический tail latency при >100 conn |

### Syscall Profiling — новые данные v5.3

`perf trace` (perf_events) заменил strace (ptrace):
- **Overhead:** <5% vs 3-13x деградация
- **FFM-совместимость:** работает (strace крашил FFM-серверы)
- **io_uring_enter видим:** acceptor — >2M вызовов за 10 сек, workers — по ~70K

### Context Switches

CS суммируются из `/proc/PID/task/*/status` (все потоки):

FFM-MT имеет в **5-9x меньше voluntary CS** благодаря io_uring batching и меньшему числу syscalls.

### FFM-MT: итоги

- **Workers = availableProcessors()** — честное сравнение с Netty.
- **SQPOLL отключён** — нет конкуренции kernel polling threads за CPU.
- **Keep-alive отключён** — одинаковый паттерн Connection: close у всех моделей.
- **Context switches:** в 5-9x меньше, чем у Netty-моделей — ключевое архитектурное преимущество io_uring batching.
- **io_uring_enter** виден через perf trace — подтверждение реального использования io_uring ring.
- **strace-независимость** — strace отключён, throughput не искажён.

### Ограничения бенчмарка

1. **Только 4c конфигурация** — масштабируемость по ядрам не тестировалась.
2. **Loopback-only** — server и client на одной машине.
3. **Netty 4.1** — результаты привязаны к конкретной версии.
4. **FFM-MT experimental** — custom event loop, не production-ready.
5. **1 run** для большинства тестов (2 runs для 100 и 1000 conn).
6. **Syscall profile** — отдельный проход, 15 тестов (5 моделей × 3 conn), не 280 основных.
"""
))

nb.cells = cells
out_path = '/ssd/benchmark/reports_v5/benchmark_analysis_v5.3.ipynb'
nbf.write(nb, out_path)
print(f"Notebook written: {out_path}")
print(f"Cells: {len(cells)}")
