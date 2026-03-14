# Методология бенчмарка I/O моделей в Linux/Java (v5.2)

## 1. Цель исследования

Сравнительный анализ производительности пяти моделей сетевого ввода-вывода в Linux на платформе JDK 21:

| Модель | Реализация | Механизм ядра |
|--------|-----------|---------------|
| Blocking I/O | Netty OIO (thread-per-connection) | `read()`/`write()` с блокировкой, поток блокируется до завершения операции |
| Non-blocking I/O | Netty NIO transport | Java NIO `Selector` → на Linux маппится на `epoll` через JDK-абстракцию |
| epoll (native) | Netty native epoll transport | Прямой `epoll_create`, `epoll_ctl`, `epoll_wait` через JNI, минуя Java NIO |
| io_uring (JNI) | Netty io_uring transport (JNI) | `io_uring_setup`, `io_uring_enter` — асинхронный I/O с SQ/CQ кольцами |
| io_uring (FFM-MT) | JDK 21 Panama FFM API, multi-threaded | `io_uring_setup`, `io_uring_enter` через FFM, многопоточный без JNI |

### Изменения относительно v4.3

**Версия v5 — полная переработка методики сбора метрик.** Все 1200 тестов перезапущены с нуля.

Исправленные проблемы:

1. **Баг PID сервера (критический).** Все системные метрики (CPU, memory, context switches, FD, syscalls) в v1-v4.3 собирались для PID Gradle wrapper, а не Java-сервера. Gradle запускает сервер через daemon-процесс, связанный с wrapper через socket (не fork). Функция `find_java_pid()` искала среди потомков wrapper PID, не находила daemon и возвращала PID самого wrapper. В результате CPU показывал ~5% для всех моделей (CPU wrapper'а), memory ~108 MB (память wrapper'а), syscalls — только futex (wrapper ждёт daemon). **Исправлено:** PID сервера определяется через `ss -tlnp` по порту. PID клиента — через `pgrep -f BenchmarkClient`.

2. **Баг расчёта CPU (критический).** `MetricsCollector` читал кумулятивные `utime`/`stime` из `/proc/pid/stat` и делил на `ticksPerSec * cpuCount`, что давало монотонно растущие значения вместо мгновенной нагрузки за секунду. **Исправлено:** хранение предыдущих значений, расчёт delta за секунду: `CPU% = (deltaTicks * 100) / (100 * cpuCount)`.

3. **Баг расчёта context switches.** Аналогично: кумулятивные `voluntary_ctxt_switches` записывались без delta. **Исправлено:** delta за секунду.

4. **Баг нормализации CPU.** `MetricsCollector` запускался через Gradle без taskset и вызывал `Runtime.getRuntime().availableProcessors()`, получая все 16 ядер машины. CPU сервера, привязанного к 4 ядрам, нормализовался на 16 — значения занижались в 4 раза. **Исправлено:** число ядер сервера передаётся как аргумент из скрипта, а не определяется автоматически.

5. **Исключена модель io_uring FFM (однопоточная).** Однопоточный FFM-сервер зависал при >10 соединениях (ring overflow), что делало данные непригодными для сравнения с многопоточными моделями.

6. **Исправлен FFM-MT сервер:**
   - Число worker'ов: `availableProcessors()` вместо `availableProcessors() / 2` — теперь равное число потоков с Netty-моделями
   - SQPOLL отключён — kernel polling thread конкурировал с worker'ами за CPU, не давая выигрыша
   - Keep-alive отключён — всегда `Connection: close`, как у остальных моделей

**Валидные метрики из v1-v4.3:** только throughput (RPS, MB/s) и latency (p50/p90/p99/p999) — они собирались клиентом, независимо от PID сервера.

### Изменения v5.2 (относительно v5)

1. **Context switches: итерация по всем потокам.** В v5 CS читались из `/proc/PID/status` (главный поток). Но Java-сервер — многопоточный процесс, и CS главного потока не отражают полную картину. В v5.2 CS суммируются из `/proc/PID/task/*/status` (все потоки процесса). Результат: CS теперь ненулевые и показывают реальные переключения (FFM-MT: 16K-38K/sec, Blocking: 89K-209K/sec, io_uring JNI: 83K-132K/sec).

2. **CPU: итерация по всем потокам.** Аналогично, CPU в v5 читался из `/proc/PID/stat` (суммарный). В v5.2 CPU суммируется из `/proc/PID/task/*/stat` для всех потоков, что даёт более точные значения при многопоточной работе.

3. **Защита от отрицательных delta.** При пересоздании потоков или race condition delta может быть отрицательной. В v5.2 добавлена проверка: `max(0, delta)`.

4. **Аномалия io_uring JNI при 100 conn.** Обнаружена воспроизводимая нестабильность: iouring_4c_100conn_4096 run1 = 8,852 RPS, run2 = 39,425 RPS (4.5x разница). Гипотезы: JVM cold start, Gradle daemon state, предыдущий тест не полностью cleanup (TIME_WAIT на порту).

### Ответы на замечания по результатам v4.3

В ходе анализа v4.3 были выявлены следующие проблемы. Для каждой указано, какое исправление в v5 её решает.

**Замечание 1: Throughput — FFM-MT хуже Blocking I/O на всех графиках.**

Blocking при 8c/512B/1000conn стабильно давал ~88K RPS, FFM-MT ~80K с коллапсом до 5K RPS к 25-й секунде. Причины:
- FFM-MT создавал `availableProcessors() / 2` worker'ов = **2 worker'а** на 4 ядра (8c config). Blocking создавал `availableProcessors() * 2` = **8 потоков**. Неравные условия.
- SQPOLL порождал 2 дополнительных kernel thread'а, которые конкурировали с worker'ами за CPU. Итого 5 потоков на 4 ядра, из которых только 2 обрабатывали трафик.
- Keep-alive у FFM-MT (остальные модели используют Connection: close) создавал другой паттерн нагрузки: FD накапливались (до 28K при 10000 conn), буферный пул исчерпывался.

**Решение в v5:** число worker'ов = `availableProcessors()`, SQPOLL отключён, keep-alive отключён. Теперь FFM-MT работает в равных условиях с Netty-моделями.

**Замечание 2: Throughput vs Data Size — FFM-MT хуже Blocking.**

Та же корневая причина — вдвое меньше worker'ов + SQPOLL overhead. Решается тем же исправлением.

**Замечание 3: Time-Series — плоские линии, аномальные пики.**

Две причины:
- **Плоские линии метрик (CPU, memory, context switches):** коллектор измерял Gradle wrapper, а не сервер. Wrapper — idle процесс с постоянными ~5% CPU и ~108 MB RSS. Отсюда плоские линии без динамики у всех моделей.
- **Плоские линии throughput у Blocking:** это корректное поведение — thread-per-connection модель с фиксированным пулом потоков даёт стабильный throughput без колебаний.
- **Аномальные пики/провалы FFM-MT:** реальный коллапс throughput из-за исчерпания буферного пула при keep-alive.

**Решение в v5:** метрики собираются для правильного PID (сервера), delta-расчёт CPU и context switches покажет реальную динамику. FFM-MT без keep-alive не будет исчерпывать буферы.

**Замечание 4: CPU Utilization — нет разницы между моделями (~5% у всех).**

Три наложившихся бага:
1. Измерялся CPU Gradle wrapper (~5% idle), а не сервера (ожидаемо 50-400%+ под нагрузкой).
2. CPU рассчитывался кумулятивно, а не как delta за секунду — значения медленно росли, а не отражали мгновенную нагрузку.
3. Нормализация на 16 ядер (все CPU машины) вместо 2-4 ядер (доступных серверу через taskset) — занижение в 4-8 раз.

**Решение в v5:** PID через `ss -tlnp`, delta-расчёт, нормализация на реальное число ядер сервера. Ожидаемый результат: blocking покажет высокий CPU при 10000 conn (thread contention), event-driven модели — более эффективное использование CPU.

**Замечание 5: Syscall Breakdown — нет разницы, SQPOLL должен снижать syscalls.**

Strace был привязан к PID Gradle wrapper. Wrapper делал ~2400 futex-вызовов (ожидание daemon) и 7 restart_syscall — одинаково для всех моделей. Реальные I/O syscalls сервера (read, write, epoll_wait, io_uring_enter) не попадали в трассировку.

**Решение в v5:** strace привязывается к реальному PID сервера. Для Netty-моделей ожидается различный профиль: blocking — read/write/accept; NIO/epoll — epoll_wait + read/write; io_uring JNI — futex (JNI скрывает io_uring_enter). Для FFM-MT strace отключён (ptrace конфликт с Panama FFM), но эта модель и раньше не использовала strace. SQPOLL отключён в v5, поэтому замечание о снижении syscalls через SQPOLL больше не актуально.

**Замечание 6: Memory — нет разницы (~100 MB у всех). Ожидалось, что FFM MemorySegment/off-heap буферы дадут эффект.**

Измерялась memory Gradle wrapper (~108 MB). Все серверы используют одинаковые JVM-параметры (`-Xms256m -Xmx2g -XX:+AlwaysPreTouch`), поэтому baseline RSS одинаков. Однако под нагрузкой:
- Blocking должен показывать рост RSS при высоких conn (стек ~1 MB на каждый поток).
- FFM-MT с fixed buffers (`IORING_REGISTER_BUFFERS`, 2048 × 4 KB = 8 MB, pre-allocated) не создаёт объектов в heap для recv — нет GC pressure.
- Netty использует ByteBuf pool (direct memory), что тоже off-heap, но с overhead на управление пулом.

**Решение в v5:** измерение RSS реального сервера покажет фактическое потребление. Ожидаемая разница: blocking RSS растёт с числом потоков; event-driven модели стабильны; FFM-MT может показать меньше GC pauses (видно в time-series).

**Замечание 7: FD — почему у FFM-MT 28000 дескрипторов при 10000 conn?**

FFM-MT использовал keep-alive (`Connection: keep-alive`), остальные модели — `Connection: close`. При keep-alive клиент открывает соединение, сервер не закрывает его после ответа. FD накапливаются: 10000 conn × ~2.8 повторных использования = ~28000 открытых FD.

**Решение в v5:** keep-alive отключён. FFM-MT будет закрывать соединение после каждого ответа, как остальные модели. FD будут масштабироваться пропорционально числу активных соединений.

**Замечание 8 (главный вопрос): Может быть дело в методике получения результатов?**

**Да.** Корневая причина отсутствия разницы между моделями — все системные метрики (CPU, memory, context switches, FD, syscalls) собирались для неправильного процесса (Gradle wrapper вместо Java-сервера). Это один баг с каскадным эффектом на все метрики. Throughput и latency были корректны (собираются клиентом), но именно системные метрики должны были показать различия в эффективности I/O моделей.

Версия v5 исправляет методику: правильный PID, delta-расчёт, корректная нормализация CPU. Все 1200 тестов перезапускаются с нуля в отдельную директорию `results_v5/`.

### Различие NIO и Epoll в контексте бенчмарка

На Linux Java NIO `Selector` внутренне использует `epoll` (начиная с JDK 1.6). Однако между Netty NIO transport и Netty native epoll transport есть существенные различия:

| Аспект | NIO transport | Native epoll transport |
|--------|--------------|----------------------|
| Syscall интерфейс | Через Java NIO (`Selector.select()`) | Прямой `epoll_wait()` через JNI |
| Промежуточные слои | JDK NIO → native → epoll | Netty JNI → epoll напрямую |
| Edge-triggered mode | Нет (level-triggered) | Да (`EPOLLET`) |
| `epoll_ctl` оптимизация | Нет | Да (Netty батчит изменения) |
| JNI overhead | NIO → JDK native → epoll | Netty JNI → epoll (один слой) |
| Поддержка `SO_REUSEPORT` | Нет | Да |

Сравнение NIO vs Epoll показывает **стоимость абстракции Java NIO** над тем же механизмом ядра.

### Различие io_uring JNI и io_uring FFM-MT

| Аспект | io_uring (JNI/Netty) | io_uring (FFM-MT) |
|--------|---------------------|-------------------|
| Вызов ядра | Через Netty native `.so` (JNI) | Через JDK 21 FFM API |
| Потоковая модель | Netty event loop (N потоков) | Acceptor + N workers |
| Число worker'ов | `availableProcessors()` | `availableProcessors()` |
| SQPOLL | Нет | Нет (отключён — конкурирует за CPU) |
| Fixed buffers | Нет (Netty ByteBuf pool) | Да (IORING_REGISTER_BUFFERS, 2048 x 4KB) |
| Backpressure | Да (Netty Channel pipeline) | Частичный (4096 entries/ring) |
| Keep-alive | Нет (Connection: close) | Нет (Connection: close) |
| Strace visibility | Только `futex` | Полный профиль syscalls |
| Зависимости | netty-transport-native-io_uring .so | Только JDK 21 (без нативных библиотек) |

### Анализ ограничений select/poll

В POSIX модели I/O multiplexing существуют три механизма: `select`, `poll` и `epoll`:

| Характеристика | select | poll | epoll |
|---------------|--------|------|-------|
| Макс. дескрипторов | `FD_SETSIZE` = 1024 (compile-time) | Без ограничений | Без ограничений |
| Сложность | O(n) — линейный скан всех FD | O(n) — линейный скан | O(1) — только готовые FD |
| Копирование FD set | Каждый вызов — полное копирование в ядро | Каждый вызов — полное копирование | Однократная регистрация через `epoll_ctl` |
| Уведомление | Level-triggered | Level-triggered | Level + Edge triggered |
| Ядро Linux | Все версии | Все версии | >= 2.6 |

## 2. Тестовое окружение

### Аппаратное обеспечение
- CPU: задокументировать модель, частоту, количество ядер (автоматически в Jupyter notebook)
- RAM: объём и тип (автоматически в Jupyter notebook)
- Storage: NVMe SSD (для логов, результатов)
- Сеть: loopback (localhost), без физического сетевого адаптера

### Программное обеспечение
- OS: Linux (ядро >= 5.10 для полной поддержки io_uring)
- JDK: OpenJDK 21
- Netty: 4.1.114.Final
- netty-io_uring: 0.0.25.Final

### Подготовка системы
- HyperThreading: отключен (используются только физические ядра 0-7)
- CPU frequency governor: `performance` (фиксированная частота)
- Kernel tuning:
  - `net.core.somaxconn = 65535`
  - `net.ipv4.tcp_max_syn_backlog = 65535`
  - `net.ipv4.ip_local_port_range = 1024 65535`
  - `net.ipv4.tcp_tw_reuse = 1`
  - `ulimit -n 1048576`

## 3. Инструменты

### Сборка и запуск
| Инструмент | Назначение |
|-----------|-----------|
| Gradle 8.12 | Сборка Java-проекта |
| OpenJDK 21 | Компиляция и запуск серверов/клиента |
| taskset | CPU pinning (привязка к ядрам) |
| bash scripts | Автоматизация запуска и сбора метрик |

### Генерация нагрузки
| Инструмент | Назначение |
|-----------|-----------|
| BenchmarkClient.java | HTTP-клиент на raw TCP сокетах |
| Virtual Threads (JDK 21) | Пул виртуальных потоков для параллельных соединений |
| HdrHistogram 2.2.2 | Сбор латентности с наносекундной точностью |

### Сбор метрик
| Инструмент | Метрика | Способ |
|-----------|---------|--------|
| `/proc/{pid}/task/*/stat` | CPU usage (user/sys %) | Delta `utime`/`stime` за секунду по всем потокам, нормализация на число ядер сервера, `max(0, delta)` |
| `/proc/{pid}/task/*/status` | Context switches (vol/invol) | Delta за секунду, суммирование по всем потокам процесса, `max(0, delta)` |
| `/proc/{pid}/status` | Memory (RSS, VSZ) | Мгновенное значение каждую секунду |
| `/proc/{pid}/fd` | Файловые дескрипторы | Подсчёт файлов каждую секунду |
| `ss -tlnp` | PID сервера | Поиск процесса, слушающего на порту сервера |
| `pgrep -f BenchmarkClient` | PID клиента | Поиск процесса по имени класса |
| `strace -f -c -S calls` | Syscalls (разбивка по типам) | Summary за весь тест (только Netty-модели) |
| `perf sched record` + `perf sched latency` | Задержки планировщика ядра | За весь тест |
| HdrHistogram (встроено в клиент) | Latency p50/p90/p99/p999 | Per-second snapshots |

### Анализ результатов
| Инструмент | Назначение |
|-----------|-----------|
| Python 3.12 | Анализ данных |
| Jupyter Notebook | Визуализация |
| pandas | Обработка CSV |
| matplotlib + seaborn | Графики |

## 4. Архитектура тестов

### Протокол
- HTTP/1.1 через raw TCP
- Каждый запрос — новое TCP-соединение (`Connection: close`)
- Запрос: `GET /data?size={N} HTTP/1.1`
- Ответ: фиксированный блок случайных данных (pre-generated, seed=42)

### Почему Connection: close
- Изолирует стоимость каждой операции: connect + write + read + close
- Показывает overhead на создание/уничтожение соединений
- Создаёт максимальную нагрузку на I/O подсистему
- Наиболее показательно для сравнения I/O моделей

### Серверы

Все серверы используют одинаковые JVM-параметры:
```
-Xms256m -Xmx2g -XX:+UseG1GC -XX:+AlwaysPreTouch --enable-preview
```

**Netty-серверы (blocking, NIO, epoll, io_uring JNI)** идентичны по логике:
1. Принять соединение
2. Прочитать HTTP запрос
3. Определить запрошенный размер данных
4. Отправить pre-generated данные в HTTP ответе
5. Закрыть соединение (Connection: close)

Потоковая модель:
| Модель | Boss threads | Worker threads | Итого |
|--------|-------------|---------------|-------|
| Blocking | — | `availableProcessors() * 2` (thread-per-connection) | 2N |
| NIO | 1 | `availableProcessors()` | N+1 |
| Epoll | 1 | `availableProcessors()` | N+1 |
| io_uring (JNI) | 1 | `availableProcessors()` | N+1 |
| io_uring (FFM-MT) | 1 acceptor | `availableProcessors()` | N+1 |

Число `availableProcessors()` определяется JVM с учётом `taskset` — при 8c конфигурации (CPUs 0-3) возвращает 4.

**FFM-MT сервер (io_uring FFM-MT):**

```
Acceptor (main thread)         Worker 0..N-1 (каждый свой поток)
+------------------+          +------------------+
| io_uring ring    |  fd ->   | io_uring ring    |
| (256 entries)    | round-   | (4096 entries)   |
| single-shot      | robin    | fixed buffers    |
| accept + re-arm  | via CLQ  | (2048 x 4KB)     |
+------------------+          | recv->send->close|
                              +------------------+
```

Оптимизации FFM-MT:

| Оптимизация | Описание | Эффект |
|-------------|----------|--------|
| Fixed buffers | `IORING_REGISTER_BUFFERS` | Нет page table lookup на recv |
| Pre-built responses | HTTP templates в `Arena.global()` | Zero per-request allocation на send |
| Per-thread rings | Каждый worker — свой ring | Нулевая контенция между потоками |
| CQ size = 2x SQ | Увеличенная CQ очередь | Защита от CQ overflow при burst |
| Non-blocking peek | `peekCompletions()` перед блокирующим wait | Нет потерь fd при blocked worker |

Что отключено:

| Функция | Причина |
|---------|---------|
| SQPOLL | Kernel polling thread конкурировал с worker'ами за CPU при ограниченном числе ядер, не давая выигрыша |
| Keep-alive | Отключён для корректного сравнения (все модели используют Connection: close) |
| Multishot accept | Ядро 6.14 возвращает EINVAL (-22) |
| SEND_ZC | Не критично для бенчмарка |
| REGISTER_FILES | Потенциальный прирост при 10K+ fd, не реализовано |

### Клиент
- Java `java.net.Socket` (raw TCP, не HttpClient)
- Виртуальные потоки (JDK 21) для генерации нагрузки
- Каждый виртуальный поток: цикл connect-send-recv-close
- Сбор latency через HdrHistogram (наносекундная точность, 3 significant digits)

## 5. Матрица параметров

### Независимые переменные

| Параметр | Значения |
|----------|---------|
| I/O модель | blocking, nio, epoll, iouring, iouring-ffm-mt |
| Параллельные соединения | 1, 10, 100, 1000, 10000 |
| Размер данных | 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB |
| CPU конфигурация | 1 ядро, 4 ядра, 8 ядер |

### CPU pinning

| Конфигурация | Серверные ядра | Клиентские ядра | Ядер у сервера |
|-------------|---------------|----------------|----------------|
| 1 ядро | CPU 0 | CPU 0 | 1 |
| 4 ядра | CPU 0-1 | CPU 2-3 | 2 |
| 8 ядер | CPU 0-3 | CPU 4-7 | 4 |

Используется `taskset -c` для привязки к конкретным физическим ядрам. HyperThreading отключен.

### Общее количество конфигураций

5 моделей × 5 connections × 8 sizes × 3 CPU configs × 2 повтора = **1200 тестов**

| Модель | Тесты |
|--------|-------|
| blocking | 240 |
| nio | 240 |
| epoll | 240 |
| iouring (JNI) | 240 |
| iouring-ffm-mt | 240 |

## 6. Протокол измерений

### Фазы теста
1. **Запуск сервера** с CPU pinning (`taskset`)
2. **Ожидание запуска** (2 секунды — компиляция Gradle + инициализация JVM)
3. **Запуск клиента** с CPU pinning (`taskset`)
4. **Warmup** (5 секунд) — прогрев JIT, стабилизация
5. **Определение PID** — сервер через `ss -tlnp` по порту, клиент через `pgrep`
6. **Активное измерение** (30 секунд) — сбор всех метрик
7. **Остановка** — завершение процессов
8. **Пауза** (5 секунд) — стабилизация ОС между тестами

### Определение PID процессов

Критически важный этап — определение реального PID Java-процесса:

| Процесс | Метод определения PID | Почему не PID Gradle wrapper |
|---------|----------------------|------------------------------|
| Сервер | `ss -tlnp \| grep :PORT` | Gradle daemon — отдельный процесс, не потомок wrapper |
| Клиент | `pgrep -f BenchmarkClient` | Клиент не слушает порт, но имя класса уникально |

### Повторы
- Каждая конфигурация выполняется 2 раза
- Результаты считаются стабильными при отклонении < 10%

### Таймаут безопасности
- Каждый тест ограничен 120 секундами
- При превышении — принудительное завершение и переход к следующему

## 7. Собираемые метрики

### Прикладные метрики (от клиента)

| Метрика | Единица | Источник |
|---------|---------|---------|
| Throughput (RPS) | req/sec | Счётчик завершённых запросов / секунда |
| Throughput (bandwidth) | MB/s | Суммарный объём данных / секунда |
| Latency p50 | us | HdrHistogram |
| Latency p90 | us | HdrHistogram |
| Latency p99 | us | HdrHistogram |
| Latency p99.9 | us | HdrHistogram |
| Latency min/max/mean/stddev | us | HdrHistogram |
| Errors | count | Счётчик ошибок соединения |

### Системные метрики (от коллектора)

| Метрика | Единица | Расчёт |
|---------|---------|--------|
| CPU user % | % за секунду | `sum(delta(utime)) * 100 / (100 * serverCpuCount)` по всем потокам `/proc/PID/task/*/stat` |
| CPU sys % | % за секунду | `sum(delta(stime)) * 100 / (100 * serverCpuCount)` по всем потокам `/proc/PID/task/*/stat` |
| Voluntary context switches | count/sec | `sum(delta(voluntary_ctxt_switches))` по всем потокам `/proc/PID/task/*/status` |
| Involuntary context switches | count/sec | `sum(delta(nonvoluntary_ctxt_switches))` по всем потокам `/proc/PID/task/*/status` |
| RSS memory | KB | Мгновенное значение `/proc/{pid}/status` VmRSS |
| VSZ memory | KB | Мгновенное значение `/proc/{pid}/status` VmSize |
| File descriptors | count | Количество файлов в `/proc/{pid}/fd` |
| Syscalls distribution | count | `strace -f -c -S calls` (summary за тест) |
| Kernel scheduling delays | ms | `perf sched record` + `perf sched latency` |

Все метрики (кроме syscalls и perf) собираются **посекундно**.

### Особенности сбора метрик

| Аспект | Netty-модели (JNI) | FFM-MT |
|--------|-------------------|--------|
| Strace | Включён (`strace -f -c`) | Отключён (ptrace-конфликт с Panama FFM) |
| CPU normalization | На число ядер сервера (из CPU config) | На число ядер сервера (из CPU config) |
| FD | PID через `ss -tlnp` | PID через `ss -tlnp` |
| Keep-alive | Нет (Connection: close) | Нет (Connection: close) |

## 8. Формат результатов

Результаты сохраняются в директорию `results_v5.2/`:

```
results_v5.2/{model}_{cpu}c_{conns}conn_{size}_run{N}/
├── throughput.csv          # per-second: RPS, MB/s
├── latency.csv             # per-second: p50, p90, p99, p999
├── cpu.csv                 # per-second: server/client user/sys % (DELTA)
├── context_switches.csv    # per-second: voluntary/involuntary (DELTA)
├── memory.csv              # per-second: RSS, VSZ
├── fd_count.csv            # per-second: open file descriptors
├── syscalls.csv            # summary: syscall breakdown (Netty only)
├── strace_raw.txt          # raw strace output (Netty only)
└── perf_sched_latency.txt  # summary: kernel scheduling delays
```

## 9. Анализ и визуализация

### Jupyter notebook

Ожидаемые секции:
1. **Throughput vs Connections** — для каждого размера данных и CPU конфигурации (5 моделей)
2. **Throughput vs Data Size** — влияние размера payload на RPS
3. **Time-series** — изменение каждой метрики по секундам
4. **Latency distribution** — boxplot p99 для каждой модели
5. **CPU utilization** — grouped bar (user + sys), ожидается различие между моделями
6. **Context switches** — voluntary/involuntary переключения, ожидается значительно больше у blocking
7. **Syscall breakdown** — анализ strace данных (Netty-модели)
8. **Memory usage** — RSS, ожидается различие при высоких connections
9. **File Descriptors** — масштабирование с числом соединений
10. **Scalability** — throughput при 1/4/8 ядрах
11. **FFM-MT vs JNI deep dive** — прямое сравнение

### Ожидаемые наблюдения (после исправления метрик)

- **Blocking**: высокий CPU и context switches при 1000+ conn (thread-per-connection); RSS растёт из-за стеков потоков
- **NIO/Epoll**: умеренный CPU; мало context switches; стабильная memory
- **io_uring (JNI)**: меньше syscalls за счёт batching SQE/CQE
- **io_uring (FFM-MT)**: сопоставимый throughput с JNI при равном числе потоков; полный strace-профиль

## 10. Сценарий воспроизведения

### Подготовка

```bash
# 1. Установить зависимости
/ssd/benchmark/scripts/setup_environment.sh

# 2. Переключить JDK на версию 21
update-alternatives --set java /usr/lib/jvm/java-21-openjdk-amd64/bin/java
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# 3. Собрать проект
cd /ssd/benchmark
./gradlew build -x test

# 4. Применить kernel tuning
sysctl -w net.core.somaxconn=65535
sysctl -w net.ipv4.tcp_max_syn_backlog=65535
sysctl -w "net.ipv4.ip_local_port_range=1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1
ulimit -n 1048576
```

### Запуск одного теста

```bash
# Формат: run_single_test.sh <model> <port> <connections> <data_size> <cpu_config> <run_number>
/ssd/benchmark/scripts/run_single_test.sh epoll 8080 1000 4096 4c 1
```

### Запуск полной матрицы (1200 тестов)

```bash
# В tmux для автономной работы
tmux new-session -d -s bench \
  'bash -c "/ssd/benchmark/scripts/run_all_benchmarks.sh 8080 2>&1 | tee /ssd/benchmark/benchmark_v5.log"'

# Мониторинг
tmux attach -t bench
# или
tail -f /ssd/benchmark/benchmark_v5.log
```

Результаты сохраняются в `results_v5.2/`. Resume-поддержка: при перезапуске скрипт пропускает уже завершённые тесты.

## 11. Ограничения

- Тестирование на loopback (не реальная сеть) — нет network latency
- Короткоживущие соединения (`Connection: close`) — не покрывает сценарий keepalive/websocket
- Один клиент — нет distributed нагрузки
- Фиксированный размер данных в каждом тесте — нет смешанной нагрузки
- JIT warmup за 5 секунд может быть недостаточен для некоторых сценариев
- strace добавляет overhead к измеряемому серверу (стоимость ptrace)
- strace для FFM-MT отключён (ptrace-конфликт с Panama FFM API)
- strace для JNI-моделей не захватывает I/O syscalls из нативных библиотек Netty (виден только futex)
- perf sched требует root-прав и может влиять на результаты при высокой нагрузке
- `AlwaysPreTouch` выделяет heap при старте — может маскировать различия в memory при коротких тестах
- При конфигурации 1c сервер и клиент делят одно ядро — CPU метрики менее информативны
- Аномалия io_uring JNI при 100 conn (cold start) — run1 может показывать до 4.5x ниже RPS, чем run2

## 12. Воспроизводимость

- Фиксированный random seed (42) для генерации данных
- Все параметры задаются через аргументы командной строки
- Скрипты автоматизируют полный цикл тестирования
- CSV-формат результатов для независимого анализа
- Конфигурация машины автоматически фиксируется в Jupyter notebook
- Telegram-уведомления о прогрессе

## 13. Исходный код модулей

| Модуль | Путь | Описание |
|--------|------|----------|
| servers/blocking-server | `servers/blocking-server/` | Netty OIO (thread-per-connection) |
| servers/nio-server | `servers/nio-server/` | Netty NIO transport |
| servers/epoll-server | `servers/epoll-server/` | Netty native epoll transport |
| servers/iouring-server | `servers/iouring-server/` | Netty io_uring transport (JNI) |
| servers/iouring-ffm-mt | `servers/iouring-ffm-mt/` | io_uring FFM (многопоточный) |
| client | `client/` | BenchmarkClient (Virtual Threads, HdrHistogram) |
| collector | `collector/` | MetricsCollector (CPU, memory, context switches, FD, strace) |

### Файлы модуля iouring-ffm-mt

```
servers/iouring-ffm-mt/
├── build.gradle.kts
└── src/main/java/benchmark/server/iouring/ffm/mt/
    ├── IoUringFfmMtServer.java   # Main: acceptor, response templates
    ├── IoUringNative.java        # FFM syscall bindings (io_uring, socket, mmap)
    ├── IoUringRing.java          # Ring abstraction (setup, mmap, getSqe, submit)
    └── WorkerThread.java         # Worker event loop (recv/send/close, fixed buffers)
```

## 14. История версий

| Версия | Моделей | Тестов | Ключевые изменения |
|--------|---------|--------|-------------------|
| v1 | 4 | 960 | Базовый анализ (blocking, nio, epoll, iouring JNI) |
| v2 | 4 | 960 | Grid layout, все размеры данных |
| v3 | 5 | 1200 | Добавлен FFM однопоточный |
| v4 | 6 | 1440 | Добавлен FFM-MT многопоточный |
| v4.1 | 6 | 1440 | Fix: FFM столбик на всех графиках |
| v4.2 | 6 | 1440 | CPU grouped bar, FFM аннотации |
| v4.3 | 6 | 1470 | Fix: FD collector (PID через ss -tlnp) |
| v5 | 5 | 1200 | Fix: все метрики (PID, CPU delta, CS delta, CPU count). Убран FFM однопоточный. Исправлен FFM-MT (threads, SQPOLL off, keep-alive off). Полный перезапуск. |
| **v5.2** | **5** | **280** | **Fix: CS по всем потокам (`/proc/PID/task/*/status`), CPU по всем потокам (`/proc/PID/task/*/stat`), защита от отрицательных delta. Аномалия io_uring JNI cold start.** |
