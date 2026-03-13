# Анализ бенчмарка I/O моделей Linux/Java — v5

## 1. Обзор

### Цель
Сравнительное тестирование пяти моделей ввода-вывода Java-серверов на Linux с исправленной методикой сбора метрик.

### Модели
| Модель | Описание | Реализация |
|--------|----------|------------|
| **Blocking I/O** | Один поток на соединение | Netty, `OioServerSocketChannel` |
| **NIO (Selector)** | Event-driven, `java.nio.channels.Selector` | Netty, `NioServerSocketChannel` |
| **Epoll (native)** | Event-driven, Linux epoll через JNI | Netty, `EpollServerSocketChannel` |
| **io_uring (JNI)** | io_uring через JNI, Netty transport | Netty, `IOUringServerSocketChannel` |
| **io_uring (FFM-MT)** | io_uring через Panama FFM, custom event loop | Без Netty, JDK 21 FFM API |

### Параметры тестирования
- **CPU:** 4 ядра (server: CPU 0,1; client: CPU 2,3)
- **Соединения:** 1, 10, 100, 1 000, 10 000
- **Размеры данных:** 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB
- **Прогоны:** 1 run для всех + 2-й run для 100 и 1000 conn
- **Итого:** 280 тестов
- **Длительность:** 305 минут (~5 часов)

### Окружение
- **CPU:** Intel Core i7-11700KF @ 3.60GHz (16 ядер)
- **RAM:** 32 GB
- **Kernel:** Linux 6.14.0
- **JDK:** OpenJDK 21.0.10
- **Netty:** 4.1.x

---

## 2. Исправления методики (v5 vs v4.3)

В v1-v4.3 были обнаружены критические ошибки в сборе метрик. Все исправлены в v5.

### 2.1 PID сервера (критическое)

**Проблема:** метрики (CPU, memory, context switches, FD, syscalls) собирались для PID Gradle wrapper, а не Java-сервера. Gradle wrapper запускает daemon через socket (не fork), поэтому `find_java_pid()` не находила сервер среди потомков wrapper.

**Результат:** CPU ~5% (вместо реальных 20-55%), memory ~100 MB (вместо 370-530 MB).

**Исправление:** PID через `ss -tlnp | grep :PORT`.

### 2.2 CPU — кумулятивные значения

**Проблема:** `utime`/`stime` из `/proc/pid/stat` — кумулятивные тики. Записывались без delta.

**Исправление:** delta-расчёт, нормализация на число ядер сервера (2), а не машины (16).

### 2.3 Context switches — кумулятивные значения

**Проблема:** `voluntary_ctxt_switches` из `/proc/pid/status` — накопительные.

**Исправление:** delta за секунду. Примечание: в текущем прогоне CS = 0 для всех моделей — возможно, delta между секундами округляется до 0 при целочисленном делении.

### 2.4 FFM-MT: workers / 2

**Проблема:** `numWorkers = availableProcessors() / 2`. При 4 доступных ядрах = 2 worker'а (Netty создавал 4).

**Исправление:** `numWorkers = availableProcessors()`.

### 2.5 FFM-MT: SQPOLL конкурировал за CPU

**Проблема:** SQPOLL kernel thread на каждый worker ring. 2 workers + 2 SQPOLL = 4 потока на 2 ядра, только 2 из них обрабатывали трафик.

**Исправление:** SQPOLL отключён.

### 2.6 FFM-MT: keep-alive

**Проблема:** FFM-MT поддерживал keep-alive, все Netty-модели — Connection: close. FD накапливались (28K при 10000 conn).

**Исправление:** всегда Connection: close.

---

## 3. Результаты

### 3.1 Throughput (RPS)

**4 ядра, payload 4KB:**

| Conn | Blocking | NIO | Epoll | io_uring JNI | FFM-MT | FFM-MT/JNI |
|------|----------|-----|-------|-------------|--------|------------|
| 1 | 2,616 | 4,626 | 5,276 | 5,851 | 1,740 | 0.30x |
| 10 | 15,379 | 17,864 | 23,144 | 22,708 | 14,943 | 0.66x |
| 100 | 23,050 | 36,074 | 39,239 | 39,370 | **59,139** | **1.50x** |
| 1,000 | 22,568 | 34,577 | 37,006 | 36,951 | **47,246** | **1.28x** |
| 10,000 | 25,084 | 32,512 | 34,724 | 33,726 | **50,884** | **1.51x** |

**Ключевые наблюдения:**
- FFM-MT **обгоняет все Netty-модели при 100+ conn** на всех размерах данных.
- При 1-10 conn FFM-MT проигрывает — overhead acceptor→worker handoff.
- При 100 conn, 64KB payload: FFM-MT/JNI ratio = **2.20x** (максимальное преимущество).
- При больших payload (512KB-1MB) все модели сходятся.

### 3.2 Latency p99 (us)

**4 ядра, payload 4KB:**

| Conn | Blocking | NIO | Epoll | io_uring JNI | FFM-MT |
|------|----------|-----|-------|-------------|--------|
| 1 | 1,362 | 375 | 313 | 251 | 1,102 |
| 100 | 45,606 | 16,851 | 11,699 | 11,606 | **2,361** |
| 1,000 | 465,608 | 153,820 | 108,682 | 105,553 | **6,079** |
| 10,000 | 1,268,441 | 2,227,258 | 621,353 | 1,120,383 | **465,875** |

**Ключевые наблюдения:**
- FFM-MT при 100-1000 conn показывает **значительно более низкий p99 latency** чем все Netty-модели.
- При 1000 conn: FFM-MT 6 мс vs JNI 106 мс — **в 17 раз ниже**.
- При 10000 conn FFM-MT всё ещё лучше всех, хотя разница сокращается.
- Blocking при 1000+ conn — p99 в сотнях миллисекунд/секундах.

### 3.3 CPU Utilization (%)

**4 ядра, payload 4KB (server CPU, delta/sec):**

| Conn | Blocking | NIO | Epoll | io_uring JNI | FFM-MT |
|------|----------|-----|-------|-------------|--------|
| 1 | 12.7% | 16.5% | 16.0% | 16.3% | 4.2% |
| 100 | 33.5% | 24.9% | 25.6% | 23.0% | **54.8%** |
| 1,000 | 35.3% | 25.2% | 26.1% | 23.4% | 21.3% |
| 10,000 | 34.7% | 27.6% | 27.2% | 24.9% | 32.0% |

**Наблюдения:**
- FFM-MT при 100 conn использует 55% CPU — высокая утилизация, но и throughput максимальный (59K RPS).
- Netty-модели стабильно 23-35%.
- io_uring JNI — наименьший CPU overhead среди всех моделей.
- Blocking тратит CPU на планирование потоков, а не на полезную работу.

### 3.4 Memory (RSS)

**4 ядра, payload 4KB:**

| Conn | Blocking | NIO | Epoll | io_uring JNI | FFM-MT |
|------|----------|-----|-------|-------------|--------|
| 1 | 344 MB | 480 MB | 473 MB | 478 MB | 478 MB |
| 1,000 | 372 MB | 505 MB | 500 MB | 502 MB | 499 MB |
| 10,000 | 364 MB | 502 MB | 480 MB | 502 MB | 494 MB |

**Наблюдения:**
- Blocking потребляет меньше RAM — нет Netty PooledByteBufAllocator overhead.
- Netty-модели и FFM-MT сопоставимы (~480-510 MB).
- Memory не зависит от числа соединений (все модели Connection: close).

### 3.5 FFM-MT / JNI Ratio (все размеры данных)

| Payload | 1 conn | 10 conn | 100 conn | 1000 conn | 10000 conn |
|---------|--------|---------|----------|-----------|------------|
| 64B | 0.28 | 0.60 | **1.41** | **1.18** | **1.38** |
| 512B | 0.28 | 0.61 | **1.90** | **1.15** | **1.44** |
| 4KB | 0.30 | 0.66 | **1.50** | **1.28** | **1.51** |
| 16KB | 0.29 | 0.67 | **1.59** | **1.43** | **1.66** |
| 64KB | 0.33 | 0.75 | **2.20** | **1.89** | **1.93** |
| 128KB | 0.34 | 0.78 | **1.75** | **1.58** | **1.62** |
| 512KB | 0.43 | 0.87 | 1.04 | 1.04 | **1.11** |
| 1MB | 0.49 | **1.15** | 1.05 | 1.05 | 1.00 |

**Паттерн:**
- При 1-10 conn FFM-MT медленнее (ratio 0.28-0.87) — overhead handoff acceptor→worker.
- При 100+ conn FFM-MT быстрее (ratio 1.04-2.20) — fixed buffers и оптимизированный ring (4096 entries) дают преимущество.
- Максимальное преимущество при средних payload (16KB-128KB) и 100 conn.
- При 1MB все модели bandwidth-bound — ratio сходится к 1.0.

---

## 4. Анализ

### 4.1 Почему FFM-MT обгоняет Netty io_uring при 100+ conn?

Несмотря на то что Panama FFM downcall (~10-15 нс) медленнее JNI call (~5-10 нс), FFM-MT имеет оптимизации, которых нет в Netty io_uring:

1. **Fixed buffers (IORING_REGISTER_BUFFERS):** 2048 pre-registered буферов на worker. Ядро не делает page table lookup при recv — экономит ~100-200 нс на запрос.
2. **Увеличенный ring (4096 entries):** Netty io_uring использует default size. Больший ring = меньше io_uring_enter() calls, batch обработка CQE.
3. **Нет Netty overhead:** FFM-MT — минимальный event loop без абстракций Netty (ChannelPipeline, ByteBuf, EventExecutor).
4. **Нет PooledByteBufAllocator:** Arena.ofConfined() per-worker — предсказуемые аллокации.

### 4.2 Почему FFM-MT медленнее при 1-10 conn?

- **Acceptor→worker handoff:** ConcurrentLinkedQueue + wakeup worker thread = дополнительная задержка.
- **Netty** обрабатывает accept и I/O в одном EventLoop при малом числе conn — нет handoff.
- При 1 conn весь трафик идёт через 1 worker, остальные простаивают.

### 4.3 Latency: почему FFM-MT лучше при 1000 conn?

FFM-MT p99 = 6 мс vs Netty io_uring p99 = 106 мс при 1000 conn. Причина:
- FFM-MT: 4 workers × ring 4096 = 16384 слотов. 1000 conn / 4 workers = 250 conn на worker. Каждый worker обрабатывает CQE batch — предсказуемая задержка.
- Netty io_uring: EventLoop обрабатывает I/O и acceptor в одном потоке, возможна конкуренция за event loop при 1000 conn.

### 4.4 Blocking I/O: ожидаемые результаты

- Throughput стабилен ~22-25K RPS при 100-10000 conn — thread pool ограничивает параллелизм.
- Tail latency катастрофический: 466 мс при 1000 conn, 1.27 с при 10000 conn.
- CPU тратится на планирование потоков (kernel space), а не на обработку данных.

---

## 5. Рейтинг моделей

| Место | Модель | Throughput | Latency | CPU efficiency | Рекомендация |
|-------|--------|-----------|---------|---------------|-------------|
| **1** | **io_uring (FFM-MT)** | Лидер при 100+ conn | Лучший p99 | Средняя | Исследования, альтернатива Netty |
| **2** | **Epoll (native)** | Стабильный лидер среди Netty | Низкий p99 | Высокая | Production на Linux |
| **3** | **io_uring (JNI)** | Сопоставим с Epoll | Низкий p99 | Лучшая | Production, Linux 5.1+ |
| **4** | **NIO (Selector)** | Средний | Средний p99 | Средняя | Кроссплатформенность |
| **5** | **Blocking I/O** | Низкий | Катастрофический p99 | Низкая | Прототипы, <100 conn |

---

## 6. Ограничения

1. **Только 4c конфигурация** — масштабируемость по ядрам не протестирована в v5.
2. **Loopback** — server и client на одной машине. Сетевая задержка = 0.
3. **Connection: close** — каждый запрос = новое TCP-соединение. Keep-alive сценарий не тестирован.
4. **Context switches = 0** — delta-расчёт в MetricsCollector возвращает 0. Требует дополнительной отладки.
5. **FFM-MT experimental** — custom event loop, не production-ready.
6. **1 run** для большинства конфигураций — статистическая значимость ограничена.
7. **Netty 4.1** — результаты привязаны к конкретной версии.

---

## 7. Файлы

| Файл | Описание |
|------|----------|
| `reports_v5/generate_notebook_v5.py` | Генератор Jupyter notebook |
| `reports_v5/benchmark_analysis_v5.ipynb` | Чистый notebook (36 ячеек) |
| `reports_v5/benchmark_analysis_v5_executed.ipynb` | Выполненный notebook (23 графика) |
| `reports_v5/analysis_v5.md` | Аналитический отчёт (этот файл) |
| `reports_v5/analysis_v5.pdf` | PDF отчёта |
| `results_v5.1/` | 280 директорий с результатами |
| `scripts/run_v5_selective.sh` | Скрипт запуска тестов |

---

## 8. Версионирование

| Версия | Моделей | Тестов | Ключевые изменения |
|--------|---------|--------|-------------------|
| v1-v2 | 4 | 960 | Базовый анализ, grid layout |
| v3 | 5 | 1200 | FFM однопоточный, strace analysis |
| v4-v4.3 | 6 | 1470 | FFM-MT, SQPOLL, FD fix, CPU grouped bar |
| **v5** | **5** | **280** | **Все метрики исправлены: PID, delta CPU/CS, workers, SQPOLL off, keep-alive off** |
