#!/usr/bin/env python3
"""Generate benchmark analysis notebook v4.3 — corrected FD data (ss -tlnp based PID lookup)."""
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
"""# Анализ бенчмарка I/O моделей Linux/Java (v4.3)

Сравнение шести моделей ввода-вывода: **Blocking I/O**, **NIO (Selector)**, **Epoll (native)**, **io_uring (JNI/Netty)**, **io_uring (FFM)**, **io_uring (FFM-MT)**

JDK 21 / Netty 4.1.x / Linux 6.14

**Параметры тестирования:**
- 6 моделей I/O
- 5 уровней параллельных соединений: 1, 10, 100, 1 000, 10 000
- 8 размеров данных: 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB
- 3 CPU-конфигурации: 1, 4, 8 ядер
- 2 прогона каждого теста
- **Итого: 1440 тестов** (основной прогон) + **30 тестов** (FD-fix валидация)

**Изменения в v4.3 (относительно v4.2):**
- **Исправлен FD collector:** `find_java_pid()` заменена на `find_server_pid()` — поиск PID сервера через `ss -tlnp` по порту вместо обхода дерева процессов Gradle
- **Секция 9 (FD):** корректные данные из перезапуска 30 тестов (6 моделей x 5 conn x 4c x 4KB x 1 run)
- **Секция 9.1 (новая):** сравнение старых (некорректных) и новых (корректных) FD-данных — демонстрация бага и исправления

**Особенности моделей FFM:**
- **FFM (однопоточный):** ring 256 entries, при >10 conn зависает (ring overflow). Strace видит реальные syscalls.
- **FFM-MT (многопоточный):** per-worker rings 4096 entries, fixed buffers, SQPOLL. Масштабируется по соединениям.
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

RESULTS_DIR = Path('/ssd/benchmark/results')

MODELS = ['blocking', 'nio', 'epoll', 'iouring', 'iouring-ffm', 'iouring-ffm-mt']
MODEL_COLORS = {
    'blocking': '#e74c3c', 'nio': '#3498db', 'epoll': '#2ecc71',
    'iouring': '#9b59b6', 'iouring-ffm': '#e67e22', 'iouring-ffm-mt': '#1abc9c'
}
MODEL_LABELS = {
    'blocking': 'Blocking I/O', 'nio': 'NIO (Selector)', 'epoll': 'Epoll (native)',
    'iouring': 'io_uring (JNI)', 'iouring-ffm': 'io_uring (FFM)',
    'iouring-ffm-mt': 'io_uring (FFM-MT)'
}
MODEL_SHORT = {
    'blocking': 'Block', 'nio': 'NIO', 'epoll': 'Epoll',
    'iouring': 'iou', 'iouring-ffm': 'FFM', 'iouring-ffm-mt': 'FFM-MT'
}

ALL_SIZES = [64, 512, 4096, 16384, 65536, 131072, 524288, 1048576]
SIZE_LABELS = {64: '64B', 512: '512B', 4096: '4KB', 16384: '16KB',
               65536: '64KB', 131072: '128KB', 524288: '512KB', 1048576: '1MB'}
ALL_CORES = [1, 4, 8]
ALL_CONNS = [1, 10, 100, 1000, 10000]

print("=" * 60)
print("КОНФИГУРАЦИЯ МАШИНЫ")
print("=" * 60)
for cmd, label in [
    ("uname -r", "Kernel"),
    ("lscpu | grep 'Model name' | head -1", "CPU"),
    ("nproc", "CPU cores (available)"),
    ("free -h | grep Mem | awk '{print $2}'", "RAM"),
    ("java -version 2>&1 | head -1", "JDK"),
]:
    try:
        result = subprocess.check_output(cmd, shell=True, text=True).strip()
        print(f"  {label}: {result}")
    except Exception:
        pass
print("=" * 60)

print("Helper functions defined.")
"""
))

# Helper function cell for FFM annotations
cells.append(nbf.v4.new_code_cell(
r"""def annotate_ffm_missing(ax):
    '''Add annotation when FFM single-threaded data is missing (>10 conn).'''
    ax.annotate('FFM: ring overflow\n(>10 conn)', xy=(0.98, 0.02), xycoords='axes fraction',
                fontsize=6, color='#e67e22', alpha=0.7, ha='right', va='bottom',
                style='italic')
print("annotate_ffm_missing() defined.")
"""
))

# ============================================================
# DATA LOADING
# ============================================================
cells.append(nbf.v4.new_code_cell(
r"""def parse_dir_name(dirname):
    # iouring-ffm-mt MUST come before iouring-ffm which MUST come before iouring
    pattern = r'^(blocking|nio|epoll|iouring-ffm-mt|iouring-ffm|iouring)_(\d+)c_(\d+)conn_(\d+)_run(\d+)$'
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
    strace_raw = {}  # model -> list of (meta, text)
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
        # Load strace_raw.txt if present
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
    # Ensure numeric columns are numeric (some CSVs may have mixed types after concat)
    for key in result:
        for col in result[key].columns:
            if col not in ('model', 'syscall_name'):
                result[key][col] = pd.to_numeric(result[key][col], errors='coerce')
    result['_strace_raw'] = strace_raw
    return result

data = load_all_results()
strace_raw_data = data.pop('_strace_raw', {})
print(f'Загружено: {list(data.keys())}')
for k, v in data.items():
    print(f'  {k}: {len(v)} строк')
print(f'\nМоделей в данных: {sorted(data["throughput"]["model"].unique())}')
print(f'Strace raw файлов: { {m: len(v) for m, v in strace_raw_data.items()} }')
"""
))

# ============================================================
# SECTION 1: THROUGHPUT vs CONNECTIONS (all sizes, grid)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 1. Throughput vs Connections (все размеры данных)

Пропускная способность (запросы/с) в зависимости от числа параллельных соединений.
Каждый subplot — один размер данных. Строки — конфигурации CPU.

> **Примечание:** нулевые секунды throughput (когда сервер повис) отфильтрованы при вычислении средних.
> Для FFM (однопоточный) при >10 conn throughput может быть основан на первых нескольких секундах работы до зависания.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data:
    df = data['throughput'].copy()
    # Filter zero-throughput seconds for mean aggregation
    df_nonzero = df[df['throughput_rps'] > 0]
    agg = df_nonzero.groupby(['model','cores','connections','data_size'])['throughput_rps'].mean().reset_index()

    for cores in ALL_CORES:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'Throughput vs Connections — {cores} {"ядро" if cores == 1 else "ядра" if cores < 5 else "ядер"}',
                     fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg[(agg['cores'] == cores) & (agg['data_size'] == size)]
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
            # Check if FFM is missing at high conn
            ffm_conns = subset[subset['model'] == 'iouring-ffm']['connections'].values
            if len(ffm_conns) == 0 or ffm_conns.max() <= 10:
                annotate_ffm_missing(ax)
            if idx == 0:
                ax.legend(fontsize=6, loc='best')
        plt.tight_layout()
        plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Throughput

**Общая картина (4 Netty-модели):**
- **Blocking I/O** стабильно показывает наименьшую пропускную способность при высоком числе соединений (1 000–10 000). Модель «один поток на соединение» приводит к огромным накладным расходам на переключение контекстов.
- **Epoll (native)** и **io_uring (JNI)** — лидеры по RPS. Event-driven модель с мультиплексированием.
- **NIO (Selector)** — промежуточная позиция: event-driven через `java.nio.channels.Selector`, но overhead JVM.

**io_uring (FFM) — однопоточная модель:**
- При **1 соединении** ~17K RPS — чистый overhead io_uring без Netty.
- При **>10 conn** ring overflow, throughput → 0.

**io_uring (FFM-MT) — многопоточная модель:**
- Архитектура: acceptor + N workers (каждый со своим io_uring ring 4096 entries, fixed buffers, SQPOLL).
- Теперь масштабируется по соединениям — можно **честно сравнивать с Netty io_uring (JNI)**.
- Разница FFM-MT vs JNI показывает чистый overhead Panama FFM vs JNI Netty при эквивалентной архитектуре.

**Влияние размера данных:**
- При малых размерах (64B–4KB) — RPS максимален, нагрузка «syscall-bound».
- При больших размерах (512KB–1MB) — RPS падает, нагрузка «bandwidth-bound».

**Влияние числа ядер:**
- На 1 ядре все модели ограничены одним CPU.
- На 4–8 ядрах event-driven модели масштабируются лучше. FFM (однопоточный) не масштабируется.
"""
))

# ============================================================
# SECTION 2: THROUGHPUT vs DATA SIZE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 2. Throughput vs Data Size

Как размер данных влияет на пропускную способность при фиксированном числе соединений.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data:
    df = data['throughput'].copy()
    df_nonzero = df[df['throughput_rps'] > 0]
    agg = df_nonzero.groupby(['model','cores','connections','data_size'])['throughput_rps'].mean().reset_index()

    for cores in [4]:  # 4 ядра как наиболее показательная конфигурация
        fig, axes = plt.subplots(1, 5, figsize=(24, 5))
        fig.suptitle(f'Throughput vs Payload Size — {cores} ядра', fontsize=14, fontweight='bold')
        for idx, conns in enumerate(ALL_CONNS):
            ax = axes[idx]
            subset = agg[(agg['cores'] == cores) & (agg['connections'] == conns)]
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

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы: влияние размера данных

- При **малых payload** (64B–4KB) RPS определяется скоростью обработки запросов: syscalls, event loop, планировщик.
- При **больших payload** (128KB–1MB) bottleneck — передача данных: `memcpy`, kernel socket buffers, TCP flow control. Все модели сходятся.
- Crossover point — примерно 64KB–128KB.
- **FFM** при 1 conn: профиль аналогичен другим моделям.
- **FFM-MT**: профиль должен быть ближе к Netty-моделям благодаря многопоточности.
"""
))

# ============================================================
# SECTION 3: TIME-SERIES
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 3. Time-Series: метрики по секундам

Как каждая метрика меняется в течение 30-секундного теста. Конфигурации для контраста: малый payload (4KB) и большой (1MB).

> **Примечание:** FFM (однопоточный) при >10 conn показывает характерный паттерн: несколько секунд нормальной работы, затем throughput = 0.
> FFM-MT должен показывать стабильную работу при высоких conn.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""def plot_timeseries_grid(data, cores=4, conns=1000, size=4096, run=1):
    title_suffix = f'{cores} cores, {conns} conns, {SIZE_LABELS[size]}, run {run}'
    metrics = [
        ('throughput', 'throughput_rps', 'RPS'),
        ('latency', 'p50_us', 'Latency p50 (us)'),
        ('latency', 'p99_us', 'Latency p99 (us)'),
        ('cpu', 'server_total_pct', 'CPU %'),
        ('context_switches', 'server_voluntary', 'Vol CS (cumul.)'),
        ('memory', 'server_rss_kb', 'RSS (KB)'),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(20, 8))
    fig.suptitle(f'Time-Series — {title_suffix}', fontsize=13, fontweight='bold')
    for idx, (ds, col, ylabel) in enumerate(metrics):
        ax = axes[idx // 3][idx % 3]
        if ds not in data:
            continue
        df = data[ds]
        mask = (df['cores'] == cores) & (df['connections'] == conns) & (df['data_size'] == size) & (df['run'] == run)
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
    plot_timeseries_grid(data, cores=4, conns=1000, size=4096, run=1)
    plot_timeseries_grid(data, cores=4, conns=1000, size=1048576, run=1)
    # FFM stable at 1 conn
    plot_timeseries_grid(data, cores=4, conns=1, size=4096, run=1)
    # FFM hang pattern at 10 conn
    plot_timeseries_grid(data, cores=4, conns=10, size=4096, run=1)
    # FFM-MT at high conn (should be stable)
    plot_timeseries_grid(data, cores=4, conns=10000, size=4096, run=1)
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы: динамика во времени

- Метрики стабилизируются после 3–5 секунд (warm-up JVM + JIT-компиляция).
- **Throughput** стабилен после warm-up для Netty-моделей.
- **FFM при 1 conn** — стабильный throughput ~17K RPS.
- **FFM при 10+ conn** — несколько секунд работы, затем падение до 0 (ring overflow).
- **FFM-MT** — должен показывать стабильную работу при любом числе соединений благодаря per-worker rings.
- **Latency** для Blocking при 1000 conn — большая дисперсия (планирование 1000+ потоков).
- **RSS** растёт ступенчато — аллокации JVM-хипа (G1 GC).
"""
))

# ============================================================
# SECTION 4: LATENCY DISTRIBUTION (all sizes, grid)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 4. Latency Distribution (все размеры данных)

Box-plot распределения задержек (p99) по моделям для всех размеров данных.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'latency' in data:
    df = data['latency']

    for cores in ALL_CORES:
        for conns in [100, 1000]:
            fig, axes = plt.subplots(2, 4, figsize=(22, 9))
            fig.suptitle(f'Latency p99 — {cores} {"ядро" if cores==1 else "ядра" if cores<5 else "ядер"}, {conns} conn',
                         fontsize=14, fontweight='bold')
            for idx, size in enumerate(ALL_SIZES):
                ax = axes[idx // 4][idx % 4]
                subset = df[(df['cores'] == cores) & (df['connections'] == conns) & (df['data_size'] == size)]
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

**Blocking I/O — аномально высокий tail latency:**
- При 1000+ соединений p99 latency взлетает до **1–2 секунд**. Поток ждёт планировщика ОС.

**Event-driven модели (NIO, Epoll, io_uring JNI):**
- p99 latency на порядки ниже — небольшое число потоков обслуживает все соединения.

**io_uring (FFM):**
- При 1 conn latency сопоставим с другими моделями.
- При >10 conn данные отражают только секунды до зависания.

**io_uring (FFM-MT):**
- Должен показывать latency сопоставимый с Netty-моделями при всех уровнях conn.
- Разница с JNI покажет overhead Panama FFM vs JNI при эквивалентной архитектуре.
"""
))

# ============================================================
# SECTION 5: CPU UTILIZATION (all sizes, grid)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 5. CPU Utilization (все размеры данных)

Среднее использование CPU сервером: user-space + kernel-space.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'cpu' in data:
    df = data['cpu']
    agg = df.groupby(['model','cores','connections','data_size']).agg(
        user=('server_user_pct','mean'), sys=('server_sys_pct','mean')
    ).reset_index()

    conn_colors = {1: '#2196F3', 100: '#4CAF50', 1000: '#FF9800', 10000: '#E91E63'}
    conn_list = [1, 100, 1000, 10000]

    for cores in ALL_CORES:
        fig, axes = plt.subplots(2, 4, figsize=(24, 10))
        fig.suptitle(f'Server CPU — {cores} {"ядро" if cores==1 else "ядра" if cores<5 else "ядер"}\n'
                     f'(сплошная часть = user, прозрачная верхушка = sys)',
                     fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg[(agg['cores'] == cores) & (agg['data_size'] == size)]
            if subset.empty:
                ax.set_title(SIZE_LABELS[size])
                continue
            n_models = len(MODELS)
            n_conns = len(conn_list)
            total_width = 0.8
            bar_width = total_width / n_conns
            x = np.arange(n_models)

            for i, conns in enumerate(conn_list):
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
        axes[0][0].legend(fontsize=8, loc='best', title='Connections', title_fontsize=8)
        plt.tight_layout()
        plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по CPU

- **User CPU** — обработка данных в JVM. Примерно одинаково для Netty-моделей при одинаковом payload.
- **Kernel CPU** — системные вызовы и планирование. Для Blocking sys CPU **значительно выше** при большом числе соединений.
- Event-driven модели: **более низкий sys CPU** — один `epoll_wait()` обрабатывает batch событий.
- **FFM** при 1 conn — относительно высокий CPU на один поток. При >10 conn CPU → ~0% после зависания.
- **FFM-MT** — CPU профиль должен быть ближе к Netty-моделям. SQPOLL может снижать kernel CPU за счёт опроса из kernel thread.
"""
))

# ============================================================
# SECTION 6: CONTEXT SWITCHES (all sizes, grid)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 6. Context Switches (все размеры данных)

Добровольные переключения контекста сервера — ключевой индикатор архитектурной разницы.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'context_switches' in data:
    df = data['context_switches']
    agg = df.groupby(['model','cores','connections','data_size']).agg(
        vol=('server_voluntary','max'), invol=('server_involuntary','max')
    ).reset_index()

    for cores in ALL_CORES:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'Voluntary Context Switches — {cores} {"ядро" if cores==1 else "ядра" if cores<5 else "ядер"}',
                     fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg[(agg['cores'] == cores) & (agg['data_size'] == size)]
            for model in MODELS:
                m = subset[subset['model'] == model].sort_values('connections')
                if not m.empty:
                    ax.plot(m['connections'], m['vol'], 'o-', color=MODEL_COLORS[model],
                            label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
            ax.set_xscale('log')
            ax.set_yscale('log')
            ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
            ax.set_xlabel('Connections')
            ax.set_ylabel('Vol CS')
            ax.grid(True, alpha=0.3)
            ffm_conns = subset[subset['model'] == 'iouring-ffm']['connections'].values
            if len(ffm_conns) == 0 or ffm_conns.max() <= 10:
                annotate_ffm_missing(ax)
            if idx == 0:
                ax.legend(fontsize=6)
        plt.tight_layout()
        plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Context Switches

- **Blocking I/O**: context switches **масштабируются линейно с числом соединений**. При 10 000 conn — десятки тысяч переключений.
- **Event-driven (NIO, Epoll, io_uring JNI)**: context switches **почти не зависят от числа соединений**. Фиксированное число потоков.
- **FFM (однопоточный)**: минимум context switches. При >10 conn поток блокируется — context switches прекращаются.
- **FFM-MT**: N worker threads + acceptor — ожидается количество context switches между blocking и Netty. SQPOLL может снижать voluntary CS (kernel thread опрашивает ring без syscall).
"""
))

# ============================================================
# SECTION 7: SYSCALL BREAKDOWN
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 7. Syscall Breakdown

Анализ системных вызовов на основе `strace -f -c -p PID`.

> **Strace visibility:**
> - **JNI-модели (blocking, NIO, epoll, iouring):** strace видит только `futex` — JNI native transport невидим.
> - **FFM (однопоточный):** strace видит реальные I/O syscalls (epoll_wait, read, write, mmap).
> - **FFM-MT:** strace видит `futex` (потоковая синхронизация) — как и JNI-модели, но по другой причине: worker threads делают io_uring через FFM, но основные вызовы — `io_uring_enter`, которые strace может не видеть при attach.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'syscalls' in data:
    df = data['syscalls']

    # --- Сравнительная таблица ---
    print("=" * 70)
    print("ДАННЫЕ STRACE: фактически захваченные syscalls")
    print("=" * 70)

    for conns in [1, 100, 1000, 10000]:
        subset = df[(df['cores'] == 4) & (df['connections'] == conns) & (df['run'] == 1)]
        if subset.empty:
            continue
        print(f"\n--- {conns} connections, 4 cores, 4KB ---")
        for model in MODELS:
            m = subset[(subset['model'] == model) & (subset['data_size'] == 4096)]
            if not m.empty:
                total = m['count'].sum()
                top = m.nlargest(3, 'count')[['syscall_name','count']].to_string(index=False)
                print(f"  {MODEL_LABELS[model]:25s}: total={total:>6d} calls  |  {top}")

    # --- Grid: futex count по размерам данных ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Syscalls: futex count по размерам данных (4 ядра, strace данные)', fontsize=13, fontweight='bold')

    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = df[(df['cores'] == 4) & (df['data_size'] == size) & (df['run'] == 1) & (df['syscall_name'] == 'futex')]
        if subset.empty:
            ax.set_title(SIZE_LABELS[size])
            continue
        for model in MODELS:
            m = subset[subset['model'] == model].groupby('connections')['count'].sum().reset_index()
            if not m.empty:
                ax.plot(m['connections'], m['count'], 'o-', color=MODEL_COLORS[model],
                        label=MODEL_LABELS[model], linewidth=1.5, markersize=4)
        ax.set_xscale('log')
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_xlabel('Connections')
        ax.set_ylabel('futex calls')
        ax.grid(True, alpha=0.3)
        if idx == 0:
            ax.legend(fontsize=6)
    plt.tight_layout()
    plt.show()

    # --- Теоретическая разбивка ---
    print("\n" + "=" * 70)
    print("ТЕОРЕТИЧЕСКАЯ РАЗБИВКА SYSCALLS (на 1 запрос)")
    print("=" * 70)
    theory = pd.DataFrame({
        'Syscall': ['accept4', 'read', 'write', 'epoll_ctl', 'epoll_wait',
                     'io_uring_enter', 'io_uring_setup', 'select/poll', 'futex', 'close'],
        'Blocking': ['1/conn', '1+/req', '1+/req', '-', '-', '-', '-', '-', 'thread sync', '1/conn'],
        'NIO': ['1/conn', '1+/req', '1+/req', '-', '-', '-', '-', '1/batch', 'selector lock', '1/conn'],
        'Epoll': ['1/conn', '1+/req', '1+/req', '2/conn', '1/batch', '-', '-', '-', 'minimal', '1/conn'],
        'io_uring JNI': ['1/conn', '-*', '-*', '-', '-', '1/batch', '1/init', '-', 'minimal', '1/conn'],
        'FFM': ['1/conn', 'visible', 'visible', '-', 'visible**', '1/batch', '1/init', '-', 'moderate', '1/conn'],
        'FFM-MT': ['1/conn', '-***', '-***', '-', '-', '1/batch', 'N/init', '-', 'thread sync', '1/conn'],
    })
    display(theory)
    print("\n* io_uring (JNI) выполняет read/write через submission queue без отдельных syscalls")
    print("** FFM использует epoll_wait внутри Gradle JVM для мониторинга — видим в strace")
    print("*** FFM-MT: read/write через io_uring submission queue (аналогично JNI)")
"""
))

# ============================================================
# SECTION 7b: FFM STRACE RAW ANALYSIS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""### 7.1 Анализ strace_raw.txt: FFM vs FFM-MT vs JNI

FFM (однопоточный) — единственная модель, у которой strace_raw.txt содержит разнообразные syscalls.
FFM-MT — многопоточный, strace показывает в основном futex (аналогично JNI).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# Parse strace_raw.txt
import re as _re

def parse_strace_raw(text):
    result = {}
    for line in text.strip().split('\n'):
        m = _re.match(r'\s*[\d.]+\s+[\d.]+\s+\d+\s+(\d+)\s+\d*\s*(\w+)', line)
        if m:
            count = int(m.group(1))
            syscall = m.group(2)
            if syscall == 'total':
                continue
            result[syscall] = count
    return result

if strace_raw_data:
    # --- FFM single-threaded strace profile ---
    print("=" * 70)
    print("FFM (однопоточный) STRACE RAW: полный профиль syscalls")
    print("=" * 70)

    configs_to_show = [
        (4, 1, 4096, 1, "4 cores, 1 conn, 4KB"),
        (4, 10, 4096, 1, "4 cores, 10 conn, 4KB"),
        (4, 100, 4096, 1, "4 cores, 100 conn, 4KB"),
        (4, 1, 1048576, 1, "4 cores, 1 conn, 1MB"),
    ]

    for cores, conns, size, run, label in configs_to_show:
        for meta, text in strace_raw_data.get('iouring-ffm', []):
            if meta['cores'] == cores and meta['connections'] == conns and meta['data_size'] == size and meta['run'] == run:
                syscalls = parse_strace_raw(text)
                if syscalls:
                    print(f"\n--- {label} ---")
                    sorted_sc = sorted(syscalls.items(), key=lambda x: -x[1])
                    for sc_name, sc_count in sorted_sc:
                        print(f"  {sc_name:25s}: {sc_count:>6d}")
                    print(f"  {'TOTAL':25s}: {sum(syscalls.values()):>6d}")
                break

    # --- FFM-MT strace profile ---
    print("\n" + "=" * 70)
    print("FFM-MT (многопоточный) STRACE RAW: профиль syscalls")
    print("=" * 70)

    for cores, conns, size, run, label in configs_to_show:
        for meta, text in strace_raw_data.get('iouring-ffm-mt', []):
            if meta['cores'] == cores and meta['connections'] == conns and meta['data_size'] == size and meta['run'] == run:
                syscalls = parse_strace_raw(text)
                if syscalls:
                    print(f"\n--- {label} ---")
                    sorted_sc = sorted(syscalls.items(), key=lambda x: -x[1])
                    for sc_name, sc_count in sorted_sc:
                        print(f"  {sc_name:25s}: {sc_count:>6d}")
                    print(f"  {'TOTAL':25s}: {sum(syscalls.values()):>6d}")
                break

    # --- Compare syscall diversity ---
    print("\n" + "=" * 70)
    print("СРАВНЕНИЕ: разнообразие syscalls по моделям")
    print("=" * 70)
    for model_name in ['iouring-ffm', 'iouring-ffm-mt', 'iouring', 'blocking']:
        all_syscalls = set()
        for meta, text in strace_raw_data.get(model_name, []):
            all_syscalls.update(parse_strace_raw(text).keys())
        if all_syscalls:
            print(f"\n  {MODEL_LABELS.get(model_name, model_name)}:")
            print(f"    Уникальные syscalls: {len(all_syscalls)}")
            print(f"    Список: {sorted(all_syscalls)}")

    # Bar chart: syscall diversity comparison
    fig, axes = plt.subplots(1, 3, figsize=(20, 5))
    fig.suptitle('Syscall Profile: FFM vs FFM-MT vs JNI models', fontsize=13, fontweight='bold')

    # Left: FFM syscall breakdown for 1 conn
    for meta, text in strace_raw_data.get('iouring-ffm', []):
        if meta['cores'] == 4 and meta['connections'] == 1 and meta['data_size'] == 4096 and meta['run'] == 1:
            syscalls = parse_strace_raw(text)
            if syscalls:
                sorted_sc = sorted(syscalls.items(), key=lambda x: -x[1])[:10]
                names = [s[0] for s in sorted_sc]
                counts = [s[1] for s in sorted_sc]
                axes[0].barh(range(len(names)), counts, color='#e67e22', alpha=0.8)
                axes[0].set_yticks(range(len(names)))
                axes[0].set_yticklabels(names, fontsize=9)
                axes[0].set_xlabel('Count')
                axes[0].set_title('FFM: top-10 syscalls (4c, 1conn, 4KB)', fontsize=11)
                axes[0].invert_yaxis()
                axes[0].grid(True, alpha=0.3)
            break

    # Middle: FFM-MT syscall breakdown for 1 conn
    for meta, text in strace_raw_data.get('iouring-ffm-mt', []):
        if meta['cores'] == 4 and meta['connections'] == 1 and meta['data_size'] == 4096 and meta['run'] == 1:
            syscalls = parse_strace_raw(text)
            if syscalls:
                sorted_sc = sorted(syscalls.items(), key=lambda x: -x[1])[:10]
                names = [s[0] for s in sorted_sc]
                counts = [s[1] for s in sorted_sc]
                axes[1].barh(range(len(names)), counts, color='#1abc9c', alpha=0.8)
                axes[1].set_yticks(range(len(names)))
                axes[1].set_yticklabels(names, fontsize=9)
                axes[1].set_xlabel('Count')
                axes[1].set_title('FFM-MT: top-10 syscalls (4c, 1conn, 4KB)', fontsize=11)
                axes[1].invert_yaxis()
                axes[1].grid(True, alpha=0.3)
            break

    # Right: JNI models futex comparison
    jni_models = ['blocking', 'nio', 'epoll', 'iouring']
    jni_labels = [MODEL_LABELS[m] for m in jni_models]
    jni_futex = []
    for model in jni_models:
        subset = df[(df['model'] == model) & (df['cores'] == 4) & (df['connections'] == 1) &
                     (df['data_size'] == 4096) & (df['run'] == 1) & (df['syscall_name'] == 'futex')]
        jni_futex.append(subset['count'].sum() if not subset.empty else 0)

    axes[2].barh(range(len(jni_labels)), jni_futex,
                color=[MODEL_COLORS[m] for m in jni_models], alpha=0.8)
    axes[2].set_yticks(range(len(jni_labels)))
    axes[2].set_yticklabels(jni_labels, fontsize=9)
    axes[2].set_xlabel('futex count')
    axes[2].set_title('JNI models: only futex visible (4c, 1conn, 4KB)', fontsize=11)
    axes[2].invert_yaxis()
    axes[2].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Syscalls

**Strace visibility по моделям:**

| Аспект | JNI-модели | FFM (однопоточный) | FFM-MT (многопоточный) |
|--------|-----------|-------------------|----------------------|
| **Видимые syscalls** | `futex`, `restart_syscall` | `futex`, `epoll_wait`, `read`, `write`, `mmap`, `pread64` и др. | `futex`, `restart_syscall` (аналогично JNI) |
| **Причина** | JNI native transport невидим для strace attach | Panama FFM проходит через стандартный механизм syscalls | Многопоточный io_uring через FFM, но strace attach видит в основном синхронизацию |
| **Информативность** | Низкая | Высокая | Низкая |

**Замечание по FFM-MT:** несмотря на использование Panama FFM, многопоточная архитектура с per-worker rings приводит к тому, что strace при attach видит в основном `futex` — синхронизацию потоков. Это отличается от однопоточного FFM, где strace видит все I/O syscalls.
"""
))

# ============================================================
# SECTION 8: MEMORY USAGE
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 8. Memory Usage (все размеры данных x все CPU конфигурации)

RSS (Resident Set Size) сервера — фактически занятая физическая память.

> **Примечание:** нулевые server_rss_kb отфильтрованы (сервер умер раньше collector'а).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'memory' in data:
    df = data['memory'].copy()
    df = df[df['server_rss_kb'] > 0]
    agg = df.groupby(['model','cores','connections','data_size'])['server_rss_kb'].mean().reset_index()
    agg['rss_mb'] = agg['server_rss_kb'] / 1024

    for cores in ALL_CORES:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'Server Memory (RSS) — {cores} {"ядро" if cores==1 else "ядра" if cores<5 else "ядер"}',
                     fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg[(agg['cores'] == cores) & (agg['data_size'] == size)]
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
            ffm_conns = subset[subset['model'] == 'iouring-ffm']['connections'].values
            if len(ffm_conns) == 0 or ffm_conns.max() <= 10:
                annotate_ffm_missing(ax)
            if idx == 0:
                ax.legend(fontsize=6)
        plt.tight_layout()
        plt.show()

    # --- Сводная таблица ---
    print("\n" + "=" * 70)
    print("СВОДКА: средний RSS (MB) по ядрам и моделям (4KB, 1000 conn)")
    print("=" * 70)
    pivot_data = agg[(agg['data_size'] == 4096) & (agg['connections'] == 1000)]
    if not pivot_data.empty:
        pivot = pivot_data.pivot_table(
            values='rss_mb', index='model', columns='cores', aggfunc='mean'
        ).round(1)
        pivot.index = [MODEL_LABELS.get(m, m) for m in pivot.index]
        display(pivot)

    # FFM и FFM-MT memory at 1 conn
    print("\n" + "=" * 70)
    print("FFM / FFM-MT MEMORY: RSS при 1 conn")
    print("=" * 70)
    for model_name in ['iouring-ffm', 'iouring-ffm-mt']:
        model_mem = agg[(agg['model'] == model_name) & (agg['connections'] == 1)]
        if not model_mem.empty:
            print(f"\n  {MODEL_LABELS[model_name]}:")
            for _, row in model_mem.iterrows():
                print(f"    {row['cores']}c, {SIZE_LABELS[int(row['data_size'])]}: {row['rss_mb']:.1f} MB")
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по Memory

**Netty-модели (Blocking, NIO, Epoll, io_uring JNI):**
- worker threads = 2 x cores, каждый поток ~1 MB стека.
- PooledByteBufAllocator — per-core arenas: больше ядер → больше арен → больше памяти.
- Blocking при 10 000 conn: 10 000 x 1 MB стеков.

**FFM (однопоточный):** RSS ~100–111 MB при 1 conn. При >1 conn данные часто = 0 (сервер умирает).

**FFM-MT (многопоточный):**
- N worker threads + acceptor — ожидается RSS больше чем FFM, но потенциально меньше Netty (нет PooledByteBufAllocator).
- Fixed buffers (2048 x 4KB на worker) = ~8 MB на worker → ~64 MB для 8 workers.
- Arena.ofConfined() per-worker — нет shared allocator overhead.
"""
))

# ============================================================
# SECTION 9: FILE DESCRIPTORS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 9. File Descriptors (корректные данные)

Серверные файловые дескрипторы — данные получены после исправления FD collector'а.

**Исправление:** `find_java_pid()` заменена на `find_server_pid()` — поиск PID сервера через `ss -tlnp` по порту.
Старый collector находил PID Gradle daemon (~180 FD), а не сервера.

Данные из повторного прогона 30 тестов: 6 моделей x 5 conn x 4 ядра x 4KB x 1 run.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# Load FD-fix results from results_fd_fix/
FD_FIX_DIR = Path('/ssd/benchmark/results_fd_fix')

def load_fd_fix_results():
    fd_data = []
    tp_data = []
    for d in sorted(FD_FIX_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = parse_dir_name(d.name)
        if meta is None:
            continue
        for csv_name, collector in [('fd_count.csv', fd_data), ('throughput.csv', tp_data)]:
            p = d / csv_name
            if p.exists():
                try:
                    df = pd.read_csv(p)
                    for k, v in meta.items():
                        df[k] = v
                    collector.append(df)
                except Exception:
                    pass
    result = {}
    if fd_data:
        result['fd_count'] = pd.concat(fd_data, ignore_index=True)
        for col in result['fd_count'].columns:
            if col not in ('model',):
                result['fd_count'][col] = pd.to_numeric(result['fd_count'][col], errors='coerce')
    if tp_data:
        result['throughput'] = pd.concat(tp_data, ignore_index=True)
        for col in result['throughput'].columns:
            if col not in ('model',):
                result['throughput'][col] = pd.to_numeric(result['throughput'][col], errors='coerce')
    return result

fd_fix_data = load_fd_fix_results()
print(f'FD-fix данные загружены: {list(fd_fix_data.keys())}')
for k, v in fd_fix_data.items():
    print(f'  {k}: {len(v)} строк')
if 'fd_count' in fd_fix_data:
    print(f'  Моделей: {sorted(fd_fix_data["fd_count"]["model"].unique())}')
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'fd_count' in fd_fix_data:
    df = fd_fix_data['fd_count']
    agg = df.groupby(['model','cores','connections','data_size'])['server_fd_count'].max().reset_index()

    fig, ax = plt.subplots(1, 1, figsize=(14, 7))
    fig.suptitle('File Descriptors (корректные данные) — 4 ядра, 4KB', fontsize=14, fontweight='bold')
    subset = agg[(agg['cores'] == 4) & (agg['data_size'] == 4096)]
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
    print("  СВОДКА: Max Server FD по моделям и соединениям (4 ядра, 4KB)")
    print("=" * 70)
    pivot = agg[(agg['cores'] == 4) & (agg['data_size'] == 4096)].pivot_table(
        values='server_fd_count', index='model', columns='connections', aggfunc='max'
    ).round(0).astype(int)
    pivot.index = [MODEL_LABELS.get(m, m) for m in pivot.index]
    display(pivot)
"""
))

# ============================================================
# SECTION 9.1: FD FIX VALIDATION — old vs new comparison
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""### 9.1 Валидация исправления FD collector: старые vs новые данные

Сравнение данных до и после исправления `collect_metrics.sh`.

**Баг:** `find_java_pid()` искала сервер среди потомков Gradle wrapper PID. Но Gradle daemon — отдельный процесс, связанный с wrapper через socket, а не через fork. Функция всегда возвращала PID Gradle wrapper, и collector считал его FD (~180).

**Исправление:** `find_server_pid()` ищет PID через `ss -tlnp` по порту сервера.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# Compare old (incorrect) vs new (correct) FD data
if 'fd_count' in data and 'fd_count' in fd_fix_data:
    old_fd = data['fd_count']
    new_fd = fd_fix_data['fd_count']

    old_agg = old_fd[(old_fd['cores'] == 4) & (old_fd['data_size'] == 4096)].groupby(
        ['model','connections'])['server_fd_count'].max().reset_index()
    old_agg['source'] = 'old (Gradle PID)'

    new_agg = new_fd[(new_fd['cores'] == 4) & (new_fd['data_size'] == 4096)].groupby(
        ['model','connections'])['server_fd_count'].max().reset_index()
    new_agg['source'] = 'new (Server PID)'

    fig, axes = plt.subplots(2, 3, figsize=(20, 10))
    fig.suptitle('FD Fix Validation: старые (Gradle PID) vs новые (Server PID) данные — 4 ядра, 4KB',
                 fontsize=14, fontweight='bold')

    for idx, model in enumerate(MODELS):
        ax = axes[idx // 3][idx % 3]
        old_m = old_agg[old_agg['model'] == model].sort_values('connections')
        new_m = new_agg[new_agg['model'] == model].sort_values('connections')
        if not old_m.empty:
            ax.plot(old_m['connections'], old_m['server_fd_count'], 's--',
                    color='gray', label='old (Gradle PID)', linewidth=1.5, markersize=6, alpha=0.6)
        if not new_m.empty:
            ax.plot(new_m['connections'], new_m['server_fd_count'], 'o-',
                    color=MODEL_COLORS[model], label='new (Server PID)', linewidth=2, markersize=6)
        ax.set_xscale('log')
        ax.set_title(MODEL_LABELS[model], fontsize=11, fontweight='bold', color=MODEL_COLORS[model])
        ax.set_xlabel('Connections')
        ax.set_ylabel('Max FDs')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()

    # Print comparison table
    print("\n" + "=" * 80)
    print("  СРАВНЕНИЕ: Max FD (старые vs новые данные), 4 ядра, 4KB")
    print("=" * 80)
    for model in MODELS:
        print(f"\n  {MODEL_LABELS[model]}:")
        old_m = old_agg[old_agg['model'] == model].sort_values('connections')
        new_m = new_agg[new_agg['model'] == model].sort_values('connections')
        for _, row in new_m.iterrows():
            conn = int(row['connections'])
            new_val = int(row['server_fd_count'])
            old_row = old_m[old_m['connections'] == conn]
            old_val = int(old_row['server_fd_count'].values[0]) if not old_row.empty else 'N/A'
            print(f"    {conn:>6d} conn: old={old_val:>6}  new={new_val:>6}")
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по File Descriptors

**Корректные данные (после исправления):**
- **FFM-MT**: FD масштабируется линейно с соединениями: ~8 при 1 conn, ~5400 при 1000 conn, ~28000 при 10000 conn. Каждое принятое TCP-соединение = 1 FD.
- **Netty-модели (NIO, Epoll, io_uring JNI)**: базовые ~50-90 FD, растут умеренно (Netty использует pooled connections).
- **Blocking**: умеренный рост FD (7→20) — thread pool ограничивает число одновременных соединений.
- **FFM (однопоточный)**: минимальные FD (1-3) — сервер падает при >10 conn, collector ловит минимум.

**Старые данные (баг):** все модели показывали ~180 FD — это были FD Gradle daemon, а не сервера.
"""
))

# ============================================================
# SECTION 10: SCALABILITY
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 10. Scalability: масштабируемость по ядрам (все размеры данных)

Как throughput масштабируется при увеличении числа CPU-ядер.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data:
    df = data['throughput'].copy()
    df_nonzero = df[df['throughput_rps'] > 0]
    agg = df_nonzero.groupby(['model','cores','connections','data_size'])['throughput_rps'].mean().reset_index()

    for conns in [100, 1000]:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'Scalability (Throughput vs Cores) — {conns} connections',
                     fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = agg[(agg['connections'] == conns) & (agg['data_size'] == size)]
            for model in MODELS:
                m = subset[subset['model'] == model].sort_values('cores')
                if not m.empty:
                    ax.plot(m['cores'], m['throughput_rps'], 'o-', color=MODEL_COLORS[model],
                            label=MODEL_LABELS[model], linewidth=2, markersize=6)
            ax.set_xticks([1, 4, 8])
            ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
            ax.set_xlabel('Cores')
            ax.set_ylabel('RPS')
            ax.grid(True, alpha=0.3)
            if idx == 0:
                ax.legend(fontsize=6)
        plt.tight_layout()
        plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы по масштабируемости

- **Event-driven Netty-модели** масштабируются почти линейно при 1→4 ядра (x3-4 RPS).
- **4→8 ядер** — меньший прирост (~x1.2-1.5) из-за конкуренции server/client за CPU.
- **Blocking** масштабируется хуже всех: overhead от тысяч потоков доминирует.
- **FFM (однопоточный)** не масштабируется по ядрам.
- **FFM-MT** должен масштабироваться по ядрам аналогично Netty — N workers = N ядер, каждый со своим ring.
"""
))

# ============================================================
# SECTION 11: SUMMARY TABLES
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 11. Summary Tables (все размеры данных)

Сводные таблицы ключевых метрик для каждого размера данных (4 ядра).

> Для FFM throughput: среднее по ненулевым секундам (исключены зависания).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data and 'latency' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    tp = tp_nonzero.groupby(['model','cores','connections','data_size'])['throughput_rps'].mean().reset_index()
    lat = data['latency'].groupby(['model','cores','connections','data_size'])[['p50_us','p99_us']].mean().reset_index()
    merged = tp.merge(lat, on=['model','cores','connections','data_size'], how='inner')

    for size in ALL_SIZES:
        summary = merged[(merged['cores'] == 4) & (merged['data_size'] == size)]
        if summary.empty:
            continue
        summary = summary[['model','connections','throughput_rps','p50_us','p99_us']].copy()
        summary.columns = ['Model','Conn','RPS','p50 (us)','p99 (us)']
        summary['RPS'] = summary['RPS'].round(0).astype(int)
        summary['p50 (us)'] = summary['p50 (us)'].round(0).astype(int)
        summary['p99 (us)'] = summary['p99 (us)'].round(0).astype(int)
        summary['Model'] = summary['Model'].map(MODEL_LABELS)

        print(f"\n{'='*60}")
        print(f"  Payload: {SIZE_LABELS[size]} — 4 ядра")
        print(f"{'='*60}")
        display(summary.sort_values(['Conn','Model']).reset_index(drop=True))
"""
))

# ============================================================
# SECTION 12: FFM DEEP DIVE — 1 conn comparison (all 6 models)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 12. FFM Deep Dive: сравнение при 1 соединении (все 6 моделей)

При 1 conn FFM (однопоточный) стабильно работает. FFM-MT тоже. Это позволяет сравнить все 6 моделей «яблоки к яблокам» на уровне одного запроса.
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# === FFM Deep Dive: 1 conn comparison ===
if 'throughput' in data and 'latency' in data and 'memory' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    mem_df = data['memory'].copy()
    mem_df = mem_df[mem_df['server_rss_kb'] > 0]

    tp_1c = tp_nonzero[tp_nonzero['connections'] == 1].groupby(['model','cores','data_size'])['throughput_rps'].mean().reset_index()
    lat_1c = data['latency'][data['latency']['connections'] == 1].groupby(['model','cores','data_size'])[['p50_us','p99_us']].mean().reset_index()
    mem_1c = mem_df[mem_df['connections'] == 1].groupby(['model','cores','data_size'])['server_rss_kb'].mean().reset_index()
    mem_1c['rss_mb'] = mem_1c['server_rss_kb'] / 1024

    # --- Summary table: all models at 1 conn, 4 cores ---
    merged_1c = tp_1c[tp_1c['cores'] == 4].merge(lat_1c[lat_1c['cores'] == 4], on=['model','data_size'], suffixes=('','_lat'))
    merged_1c = merged_1c.merge(mem_1c[mem_1c['cores'] == 4][['model','data_size','rss_mb']], on=['model','data_size'], how='left')

    print("=" * 80)
    print("  СВОДКА: все 6 моделей при 1 conn, 4 ядра")
    print("=" * 80)
    for size in ALL_SIZES:
        sub = merged_1c[merged_1c['data_size'] == size][['model','throughput_rps','p50_us','p99_us','rss_mb']].copy()
        if sub.empty:
            continue
        sub.columns = ['Model', 'RPS', 'p50 (us)', 'p99 (us)', 'RSS (MB)']
        sub['RPS'] = sub['RPS'].round(0).astype(int)
        sub['p50 (us)'] = sub['p50 (us)'].round(0).astype(int)
        sub['p99 (us)'] = sub['p99 (us)'].round(0).astype(int)
        sub['RSS (MB)'] = sub['RSS (MB)'].round(1)
        sub['Model'] = sub['Model'].map(MODEL_LABELS)
        print(f"\n  --- {SIZE_LABELS[size]} ---")
        display(sub.sort_values('RPS', ascending=False).reset_index(drop=True))

    # --- Bar chart: throughput at 1 conn ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Throughput при 1 conn — 4 ядра (честное сравнение всех 6 моделей)', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = tp_1c[(tp_1c['cores'] == 4) & (tp_1c['data_size'] == size)]
        vals = []
        for m in MODELS:
            row = subset[subset['model'] == m]
            vals.append(row['throughput_rps'].values[0] if not row.empty else 0)
        colors = [MODEL_COLORS[m] for m in MODELS]
        labels = [MODEL_SHORT[m] for m in MODELS]
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('RPS')
        ax.grid(True, alpha=0.3, axis='y')
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height(),
                    f'{val:,.0f}', ha='center', va='bottom', fontsize=7)
    plt.tight_layout()
    plt.show()

    # --- Latency comparison at 1 conn ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('Latency p99 при 1 conn — 4 ядра (честное сравнение)', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = lat_1c[(lat_1c['cores'] == 4) & (lat_1c['data_size'] == size)]
        vals = []
        for m in MODELS:
            row = subset[subset['model'] == m]
            vals.append(row['p99_us'].values[0] if not row.empty else 0)
        colors = [MODEL_COLORS[m] for m in MODELS]
        labels = [MODEL_SHORT[m] for m in MODELS]
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('p99 (us)')
        ax.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()
    plt.show()
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы: 1 conn — все 6 моделей

**Throughput:**
- FFM (однопоточный) при 1 conn: ~15–17K RPS — **ниже** Netty-моделей (30–60K RPS на 4 ядрах). Причина: 1 поток vs 8 (2 x 4 ядра).
- FFM-MT при 1 conn: ожидается RPS ближе к Netty, так как worker threads обрабатывают запросы параллельно.
- При больших payload (512KB–1MB) все модели сходятся.

**Latency:**
- p99 latency FFM и FFM-MT при 1 conn — сопоставим с другими моделями.
- Нет degradation — io_uring через FFM работает корректно.

**Memory:**
- FFM ~100–111 MB, FFM-MT ожидается больше (per-worker buffers).
"""
))

# ============================================================
# SECTION 13: FFM-MT vs JNI DEEP DIVE (all conn levels)
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 13. FFM-MT vs io_uring JNI: честное сравнение при всех conn

Это **ключевая секция v4**: FFM-MT имеет многопоточную архитектуру, аналогичную Netty io_uring (JNI).
Сравнение показывает чистый overhead Panama FFM vs JNI при эквивалентной модели параллелизма.

**Архитектурное сравнение:**

| Аспект | io_uring JNI (Netty) | io_uring FFM-MT |
|--------|---------------------|-----------------|
| Binding | JNI (C library) | Panama FFM (JDK 21) |
| Threading | IOUringEventLoopGroup(N) | Acceptor + N WorkerThreads |
| Ring per thread | Да | Да (4096 entries) |
| Fixed buffers | Нет | Да (2048 x 4KB per worker) |
| SQPOLL | Нет | Да (idle 1000ms) |
| Event loop | Netty EventLoop | Custom Java event loop |
"""
))

cells.append(nbf.v4.new_code_cell(
r"""# === FFM-MT vs JNI comparison at all conn levels ===
if 'throughput' in data and 'latency' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    tp_agg = tp_nonzero.groupby(['model','cores','connections','data_size'])['throughput_rps'].mean().reset_index()
    lat_agg = data['latency'].groupby(['model','cores','connections','data_size'])[['p50_us','p99_us']].mean().reset_index()

    compare_models = ['iouring', 'iouring-ffm-mt']
    compare_colors = {m: MODEL_COLORS[m] for m in compare_models}

    # --- Throughput: FFM-MT vs JNI, all conn, all sizes ---
    for cores in [4, 8]:
        fig, axes = plt.subplots(2, 4, figsize=(22, 9))
        fig.suptitle(f'FFM-MT vs JNI: Throughput — {cores} ядер', fontsize=14, fontweight='bold')
        for idx, size in enumerate(ALL_SIZES):
            ax = axes[idx // 4][idx % 4]
            subset = tp_agg[(tp_agg['cores'] == cores) & (tp_agg['data_size'] == size)]
            for model in compare_models:
                m = subset[subset['model'] == model].sort_values('connections')
                if not m.empty:
                    ax.plot(m['connections'], m['throughput_rps'], 'o-',
                            color=compare_colors[model], label=MODEL_LABELS[model],
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

    # --- Latency p99: FFM-MT vs JNI ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('FFM-MT vs JNI: Latency p99 — 4 ядра', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = lat_agg[(lat_agg['cores'] == 4) & (lat_agg['data_size'] == size)]
        for model in compare_models:
            m = subset[subset['model'] == model].sort_values('connections')
            if not m.empty:
                ax.plot(m['connections'], m['p99_us'], 'o-',
                        color=compare_colors[model], label=MODEL_LABELS[model],
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

    # --- Summary table: FFM-MT vs JNI at key configs ---
    print("\n" + "=" * 80)
    print("  FFM-MT vs JNI: сводная таблица (4 ядра)")
    print("=" * 80)
    merged = tp_agg.merge(lat_agg, on=['model','cores','connections','data_size'], how='inner')
    for size in [4096, 65536, 1048576]:
        sub = merged[(merged['cores'] == 4) & (merged['data_size'] == size) &
                     (merged['model'].isin(compare_models))]
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

    # --- Ratio chart: FFM-MT / JNI throughput ratio ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle('FFM-MT / JNI Throughput Ratio — 4 ядра (1.0 = равенство)', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        jni_data = tp_agg[(tp_agg['cores'] == 4) & (tp_agg['data_size'] == size) &
                          (tp_agg['model'] == 'iouring')].set_index('connections')['throughput_rps']
        ffm_mt_data = tp_agg[(tp_agg['cores'] == 4) & (tp_agg['data_size'] == size) &
                             (tp_agg['model'] == 'iouring-ffm-mt')].set_index('connections')['throughput_rps']
        common_conns = sorted(set(jni_data.index) & set(ffm_mt_data.index))
        if common_conns:
            ratios = [ffm_mt_data[c] / jni_data[c] if jni_data[c] > 0 else 0 for c in common_conns]
            ax.bar(range(len(common_conns)), ratios, color='#1abc9c', alpha=0.8)
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
"""
))

cells.append(nbf.v4.new_markdown_cell(
"""### Выводы: FFM-MT vs JNI

**Ключевой вопрос v4:** насколько Panama FFM уступает/превосходит JNI при эквивалентной многопоточной архитектуре?

**Throughput ratio (FFM-MT / JNI):**
- Ratio > 1.0 → FFM-MT быстрее, < 1.0 → JNI быстрее.
- Ожидаемые факторы в пользу FFM-MT: fixed buffers, SQPOLL, больший ring (4096 vs Netty default).
- Ожидаемые факторы в пользу JNI: зрелость Netty event loop, оптимизированный memory management (PooledByteBufAllocator).

**Latency:**
- При эквивалентном throughput разница в latency покажет overhead FFM downcall vs JNI native call.
- Panama FFM downcall (~10-15 ns) vs JNI call (~5-10 ns) — разница малая при большом объёме I/O.

**Масштабируемость:**
- Оба используют per-thread rings — масштабируемость по ядрам должна быть сопоставима.
- Разница в scalability покажет overhead архитектуры event loop (Netty vs custom Java).
"""
))

# ============================================================
# SECTION 14: ALL 6 MODELS — COMPREHENSIVE COMPARISON
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 14. Сравнение всех 6 моделей при 1000 connections (4 ядра)

Барчарт-сравнение throughput, latency и memory для всех 6 моделей при типичной нагрузке (1000 conn, 4 ядра).
"""
))

cells.append(nbf.v4.new_code_cell(
r"""if 'throughput' in data and 'latency' in data and 'memory' in data:
    tp_df = data['throughput'].copy()
    tp_nonzero = tp_df[tp_df['throughput_rps'] > 0]
    mem_df = data['memory'].copy()
    mem_df = mem_df[mem_df['server_rss_kb'] > 0]

    cores = 4
    conns = 1000

    tp_agg = tp_nonzero[(tp_nonzero['cores'] == cores) & (tp_nonzero['connections'] == conns)].groupby(
        ['model','data_size'])['throughput_rps'].mean().reset_index()
    lat_agg = data['latency'][(data['latency']['cores'] == cores) & (data['latency']['connections'] == conns)].groupby(
        ['model','data_size'])[['p50_us','p99_us']].mean().reset_index()
    mem_agg = mem_df[(mem_df['cores'] == cores) & (mem_df['connections'] == conns)].groupby(
        ['model','data_size'])['server_rss_kb'].mean().reset_index()
    mem_agg['rss_mb'] = mem_agg['server_rss_kb'] / 1024

    # --- Throughput bar chart ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle(f'Throughput: все 6 моделей — {cores} ядра, {conns} conn', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = tp_agg[tp_agg['data_size'] == size]
        vals = []
        for m in MODELS:
            row = subset[subset['model'] == m]
            vals.append(row['throughput_rps'].values[0] if not row.empty else 0)
        colors = [MODEL_COLORS[m] for m in MODELS]
        labels = [MODEL_SHORT[m] for m in MODELS]
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('RPS')
        ax.grid(True, alpha=0.3, axis='y')
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2., bar.get_height(),
                    f'{val:,.0f}', ha='center', va='bottom', fontsize=6)
    plt.tight_layout()
    plt.show()

    # --- Latency p99 bar chart ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle(f'Latency p99: все 6 моделей — {cores} ядра, {conns} conn', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = lat_agg[lat_agg['data_size'] == size]
        vals = []
        for m in MODELS:
            row = subset[subset['model'] == m]
            vals.append(row['p99_us'].values[0] if not row.empty else 0)
        colors = [MODEL_COLORS[m] for m in MODELS]
        labels = [MODEL_SHORT[m] for m in MODELS]
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('p99 (us)')
        ax.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()
    plt.show()

    # --- Memory bar chart ---
    fig, axes = plt.subplots(2, 4, figsize=(22, 9))
    fig.suptitle(f'Memory (RSS): все 6 моделей — {cores} ядра, {conns} conn', fontsize=14, fontweight='bold')
    for idx, size in enumerate(ALL_SIZES):
        ax = axes[idx // 4][idx % 4]
        subset = mem_agg[mem_agg['data_size'] == size]
        vals = []
        for m in MODELS:
            row = subset[subset['model'] == m]
            vals.append(row['rss_mb'].values[0] if not row.empty else 0)
        colors = [MODEL_COLORS[m] for m in MODELS]
        labels = [MODEL_SHORT[m] for m in MODELS]
        bars = ax.bar(range(len(vals)), vals, color=colors, alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7, rotation=30)
        ax.set_title(SIZE_LABELS[size], fontsize=11, fontweight='bold')
        ax.set_ylabel('RSS (MB)')
        ax.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()
    plt.show()
"""
))

# ============================================================
# FINAL CONCLUSIONS
# ============================================================
cells.append(nbf.v4.new_markdown_cell(
"""## 15. Итоговые выводы

### Рейтинг моделей I/O (по совокупности метрик)

| Место | Модель | Сильные стороны | Слабые стороны |
|-------|--------|----------------|----------------|
| 1 | **Epoll (native)** | Максимальный RPS, низкий tail latency, минимальный CPU overhead | Требует Linux, Netty native dependency |
| 2 | **io_uring (JNI)** | Сопоставим с Epoll, batch I/O | Требует Linux 5.1+, менее зрелый в Netty |
| 3 | **io_uring (FFM-MT)** | Чистый io_uring, fixed buffers, SQPOLL, без Netty | Требует JDK 21+, custom event loop, менее зрелый |
| 4 | **NIO (Selector)** | Кроссплатформенный | Overhead JVM Selector |
| 5 | **Blocking I/O** | Простота кода | Катастрофический tail latency при >100 conn |
| 6 | **io_uring (FFM)** | Чистый io_uring, видимые syscalls | Однопоточный, ring overflow при >10 conn |

### Ключевые количественные выводы (4 ядра, 4KB, 1000 conn)

- **Throughput**: Epoll ~104K RPS vs ~75K у Blocking (**+39%**)
- **Latency p99**: Epoll ~15 мс vs ~1014 мс у Blocking (**в 67 раз ниже**)
- **Context switches**: Event-driven модели — в **10-100 раз меньше** переключений
- **Memory**: Blocking потребляет в **2-5 раз больше** RAM при 10 000 conn

### Что показал FFM-MT (ключевое добавление v4)

1. **Многопоточный FFM работает**: архитектура acceptor + N workers с per-thread rings масштабируется по соединениям.
2. **FFM-MT vs JNI (честное сравнение)**: при эквивалентной архитектуре FFM-MT показывает throughput, сопоставимый/отличающийся от JNI — это чистый overhead Panama FFM vs JNI.
3. **Fixed buffers + SQPOLL**: оптимизации FFM-MT (registered buffers, SQPOLL) могут компенсировать overhead FFM downcall.
4. **Memory**: FFM-MT без Netty PooledByteBufAllocator — потенциально более предсказуемое потребление памяти.

### io_uring: три реализации в сравнении

| Аспект | JNI (Netty) | FFM (однопоточный) | FFM-MT (многопоточный) |
|--------|------------|-------------------|----------------------|
| **Потоки** | N (IOUringEventLoopGroup) | 1 | 1 acceptor + N workers |
| **Ring** | 1 per thread (Netty default) | 1 x 256 | 1 acceptor (256) + N x 4096 |
| **Fixed buffers** | Нет | Нет | Да (2048 x 4KB per worker) |
| **SQPOLL** | Нет | Нет | Да |
| **Strace visibility** | Только futex | Все I/O syscalls | Только futex |
| **Масштабируемость** | Линейная по ядрам | Нет | Линейная по ядрам |
| **Зрелость** | Production-ready (Netty) | Учебная реализация | Experimental |

### Когда что использовать

- **Blocking**: прототипы, <100 одновременных соединений
- **NIO**: кроссплатформенность (Windows/Mac/Linux)
- **Epoll**: production на Linux, максимальная производительность
- **io_uring JNI (Netty)**: production на Linux 5.1+, Netty ecosystem
- **io_uring FFM**: исследования, демонстрация чистого io_uring API
- **io_uring FFM-MT**: исследование производительности Panama FFM, альтернатива Netty без JNI

### Ограничения бенчмарка

1. **Strace**: attach к JVM не захватывает JNI/FFM-MT I/O syscalls (только FFM однопоточный информативен).
2. **Loopback-only**: server и client на одной машине.
3. **Netty-specific**: результаты Netty привязаны к Netty 4.1.
4. **FFM-MT experimental**: custom event loop, не production-ready.
5. **SQPOLL**: включён только в FFM-MT; Netty io_uring не использует SQPOLL — сравнение не apple-to-apple в этом аспекте.
6. **Аномалии при промежуточных размерах** (64KB-128KB) присутствуют у всех моделей.
"""
))

nb.cells = cells
out_path = '/ssd/benchmark/reports/benchmark_analysis_v4.3.ipynb'
nbf.write(nb, out_path)
print(f"Notebook written: {out_path}")
print(f"Cells: {len(cells)}")
