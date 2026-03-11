# Методология бенчмарка I/O моделей в Linux/Java (v4.3)

## 1. Цель исследования

Сравнительный анализ производительности шести моделей сетевого ввода-вывода в Linux на платформе JDK 21:

| Модель | Реализация | Механизм ядра |
|--------|-----------|---------------|
| Blocking I/O | Netty OIO (thread-per-connection) | `read()`/`write()` с блокировкой, поток блокируется до завершения операции |
| Non-blocking I/O | Netty NIO transport | Java NIO `Selector` → на Linux маппится на `epoll` через JDK-абстракцию |
| epoll (native) | Netty native epoll transport | Прямой `epoll_create`, `epoll_ctl`, `epoll_wait` через JNI, минуя Java NIO |
| io_uring (JNI) | Netty io_uring transport (JNI) | `io_uring_setup`, `io_uring_enter` — асинхронный I/O с SQ/CQ кольцами |
| io_uring (FFM) | JDK 21 Panama FFM API | `io_uring_setup`, `io_uring_enter` через FFM без JNI, однопоточный сервер |
| io_uring (FFM-MT) | JDK 21 Panama FFM API, multi-threaded | `io_uring_setup`, `io_uring_enter` через FFM, многопоточный с SQPOLL |

### Изменения относительно v4.2

**v4 (относительно v3):** добавлена многопоточная FFM-реализация (`iouring-ffm-mt`), использующая ту же архитектуру «один ring на поток», что и Netty io_uring. Это позволяет корректно сравнивать FFM и JNI при одинаковом уровне параллелизма.

**v4.2 (относительно v4):**
- **Исправлен график CPU (секция 5):** stacked bar заменён на grouped bar — столбики для разных уровней connections стоят рядом, user-часть сплошная, sys-часть прозрачная сверху. В v4 stacked bar был нечитаем: все модели показывали одинаковые ~4-5% CPU при 1 ядре, разница между моделями и уровнями connections была неразличима. Grouped bar позволяет видеть, что blocking при 10000 conn потребляет значительно больше CPU, чем event-driven модели.
- **Добавлены аннотации FFM** на графиках throughput, context switches и memory — текст «FFM: ring overflow (>10 conn)» там, где данные FFM однопоточного отсутствуют.
- **Секция FD помечена** как содержащая некорректные данные (см. ниже).

**v4.3 (относительно v4.2):**
- **Исправлен FD collector (секция 9):** функция `find_java_pid()` в `collect_metrics.sh` заменена на `find_server_pid()` — поиск PID сервера через `ss -tlnp` по порту вместо обхода дерева процессов Gradle. **Суть бага:** Gradle запускает сервер через daemon-процесс, который связан с wrapper через socket, а не через fork. Старая функция `find_java_pid()` искала серверный процесс среди потомков Gradle wrapper PID — но daemon не является потомком wrapper. В результате функция возвращала PID самого Gradle wrapper, и collector считал его FD (~180 для всех моделей, при любом числе соединений). На графиках FD все 6 моделей показывали плоскую линию ~180 FD — это были файловые дескрипторы Gradle daemon (classpath JAR'ы, JIT temp files), а не серверные сокеты.
- **Корректные FD-данные:** после исправления FD масштабируются с числом соединений: FFM-MT показывает ~8 FD при 1 conn и ~28000 при 10000 conn (каждое TCP-соединение = 1 FD), Netty-модели ~50-90 FD (event loop), Blocking ~7-20 FD (thread pool ограничивает).
- **Добавлена опция `noStrace`** в `MetricsCollector.java` для FFM-моделей — strace вызывал ptrace-конфликты с FFM Panama.
- **Перезапущены 30 тестов** (6 моделей x 5 conn x 4c x 4KB x 1 run) с корректным FD collector. Результаты в `results_fd_fix/`.
- **Notebook v4.3:** секция 9 — корректные FD-данные из `results_fd_fix/`; секция 9.1 — сравнение старых (Gradle PID, ~180 FD) и новых (Server PID, реальное масштабирование) данных на одном графике для каждой модели.
- **Итого:** 1440 тестов (основной прогон) + 30 тестов (FD-fix валидация) = **1470 тестов**.

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

Таким образом, сравнение NIO vs Epoll показывает **стоимость абстракции Java NIO** над тем же механизмом ядра.

### Различие io_uring JNI и io_uring FFM

| Аспект | io_uring (JNI/Netty) | io_uring (FFM) | io_uring (FFM-MT) |
|--------|---------------------|----------------|-------------------|
| Вызов ядра | Через Netty native `.so` (JNI) | Через JDK 21 FFM API | Через JDK 21 FFM API |
| Потоковая модель | Netty event loop (N потоков) | Однопоточный | Acceptor + N workers |
| SQPOLL | Нет | Нет | Да (kernel-side SQ polling) |
| Fixed buffers | Нет (Netty ByteBuf) | Нет | Да (IORING_REGISTER_BUFFERS) |
| Backpressure | Да (Netty Channel pipeline) | Нет (ring overflow) | Частичный (4096 entries/ring) |
| Strace visibility | Только `futex` | Полный профиль syscalls | Полный профиль syscalls |
| Масштабируемость | Линейная до 4-8 ядер | Не масштабируется | Линейная (1 ring/worker) |

### Анализ ограничений select/poll

В POSIX модели I/O multiplexing существуют три механизма: `select`, `poll` и `epoll`. В данном бенчмарке мы используем epoll как наиболее эффективный, но важно понимать ограничения предшественников:

| Характеристика | select | poll | epoll |
|---------------|--------|------|-------|
| Макс. дескрипторов | `FD_SETSIZE` = 1024 (compile-time) | Без ограничений | Без ограничений |
| Сложность | O(n) — линейный скан всех FD | O(n) — линейный скан | O(1) — только готовые FD |
| Копирование FD set | Каждый вызов — полное копирование в ядро | Каждый вызов — полное копирование | Однократная регистрация через `epoll_ctl` |
| Уведомление | Level-triggered | Level-triggered | Level + Edge triggered |
| Ядро Linux | Все версии | Все версии | >= 2.6 |

**Ключевые ограничения select:**
- Жёсткий лимит `FD_SETSIZE = 1024` — невозможно обрабатывать > 1024 соединений
- На Linux Java NIO `Selector` **не использует select** — всегда epoll (начиная с JDK 1.6)
- Для демонстрации ограничения select потребовался бы C-код, что выходит за рамки Java-бенчмарка

**poll** решает проблему лимита FD, но сохраняет O(n) сложность — при 10000 соединений каждый вызов `poll()` проходит по всему массиву дескрипторов.

**epoll** решает обе проблемы: нет лимита на FD, O(1) на возврат готовых событий, однократная регистрация дескрипторов.

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
| `/proc/{pid}/stat` | CPU usage (user/sys %) | Чтение каждую секунду |
| `/proc/{pid}/status` | Context switches (vol/invol), Memory (RSS, VSZ) | Чтение каждую секунду |
| `/proc/{pid}/fd` | Файловые дескрипторы | Подсчёт файлов каждую секунду (PID сервера определяется через `ss -tlnp` по порту) |
| `strace -c -S calls` | Syscalls (разбивка по типам) | Summary за весь тест |
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
- HTTP/1.1 по умолчанию использует keep-alive, но для бенчмарка короткоживущие соединения лучше демонстрируют разницу

### Серверы

**Netty-серверы (blocking, NIO, epoll, io_uring JNI)** идентичны по логике:
1. Принять соединение
2. Прочитать HTTP запрос
3. Определить запрошенный размер данных
4. Отправить pre-generated данные в HTTP ответе
5. Закрыть соединение

**FFM-сервер (io_uring FFM) — однопоточный:**
1. Инициализация io_uring через Panama Foreign Function API (`io_uring_setup`)
2. Регистрация server socket в ring
3. Однопоточный цикл: `io_uring_enter` → обработка CQE → submit новых SQE
4. Нет event loop — один поток обрабатывает все операции
5. Нет backpressure — при перегрузке ring переполняется

**FFM-MT сервер (io_uring FFM-MT) — многопоточный:**
1. Pre-build HTTP response templates в `Arena.global()` (zero per-request alloc)
2. Acceptor thread (main): собственный io_uring ring (256 entries), single-shot accept с re-arm
3. Round-robin распределение fd по N worker threads через `ConcurrentLinkedQueue`
4. Каждый worker: свой io_uring ring (4096 entries), fixed buffers (2048 x 4KB), SQPOLL
5. Worker event loop: неблокирующий peek → обработка CQE → drain new fds → блокирующий wait только при idle
6. Partial send handling для ответов > 4KB
7. Keep-alive поддержка (парсинг Connection header)

Данные генерируются один раз при старте сервера с фиксированным seed для воспроизводимости.

### Архитектура FFM-MT сервера

```
Acceptor (main thread)         Worker 0..N-1 (каждый свой поток)
+------------------+          +------------------+
| io_uring ring    |  fd ->   | io_uring ring    |
| (256 entries)    | round-   | (4096 entries)   |
| single-shot      | robin    | SQPOLL enabled   |
| accept + re-arm  | via CLQ  | fixed buffers    |
+------------------+          | (2048 x 4KB)     |
                              | recv->send->close|
                              +------------------+
```

**Оптимизации FFM-MT:**

| Оптимизация | Описание | Эффект |
|-------------|----------|--------|
| SQPOLL | Kernel-side SQ polling thread | Нет syscall на submit (io_uring_enter) |
| Fixed buffers | `IORING_REGISTER_BUFFERS` | Нет page table lookup на recv |
| Pre-built responses | HTTP templates в `Arena.global()` | Zero per-request allocation на send |
| Per-thread rings | Каждый worker — свой ring | Нулевая контенция между потоками |
| CQ size = 2x SQ | Увеличенная CQ очередь | Защита от CQ overflow при burst |
| Non-blocking event loop | `peekCompletions()` перед блокирующим wait | Нет потерь fd при blocked worker |

**Что НЕ реализовано (отложено):**

| Функция | Причина |
|---------|---------|
| Multishot accept | Ядро 6.14 возвращает EINVAL (-22) |
| SEND_ZC (zero-copy send) | Не критично для бенчмарка |
| Registered files (REGISTER_FILES) | Потенциальный прирост при 10K+ fd, не реализовано |

### Клиент
- Java `java.net.Socket` (raw TCP, не HttpClient)
- Виртуальные потоки (JDK 21) для генерации нагрузки
- Каждый виртуальный поток: цикл connect-send-recv-close
- Сбор latency через HdrHistogram (наносекундная точность, 3 significant digits)

## 5. Матрица параметров

### Независимые переменные

| Параметр | Значения |
|----------|---------|
| I/O модель | blocking, nio, epoll, iouring, iouring-ffm, iouring-ffm-mt |
| Параллельные соединения | 1, 10, 100, 1000, 10000 |
| Размер данных | 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB |
| CPU конфигурация | 1 ядро, 4 ядра, 8 ядер |

### CPU pinning

| Конфигурация | Серверные ядра | Клиентские ядра |
|-------------|---------------|----------------|
| 1 ядро | CPU 0 | CPU 0 |
| 4 ядра | CPU 0-1 | CPU 2-3 |
| 8 ядер | CPU 0-3 | CPU 4-7 |

Используется `taskset -c` для привязки к конкретным физическим ядрам. HyperThreading отключен — используются только ядра 0-7 (без SMT-пар 8-15).

### Общее количество конфигураций
6 x 5 x 8 x 3 x 2 повтора = **1440 тестов** (основной прогон) + **30 тестов** (FD-fix валидация) = **1470 тестов**

| Модель | Тесты |
|--------|-------|
| blocking | 240 |
| nio | 240 |
| epoll | 240 |
| iouring (JNI) | 240 |
| iouring-ffm | 240 |
| iouring-ffm-mt | 240 |

## 6. Протокол измерений

### Фазы теста
1. **Запуск сервера** с CPU pinning (`taskset`)
2. **Warmup** (5 секунд) — прогрев JIT, стабилизация
3. **Активное измерение** (30 секунд) — сбор всех метрик
4. **Остановка** — завершение процессов
5. **Пауза** (5 секунд) — стабилизация ОС между тестами

### Повторы
- Каждая конфигурация выполняется 2 раза
- Результаты считаются стабильными при отклонении < 10%
- При большем отклонении — дополнительный прогон

### Таймаут безопасности
- Каждый тест ограничен 120 секундами
- При превышении — принудительное завершение и переход к следующему

## 7. Собираемые метрики

### Прикладные метрики (от клиента)

| Метрика | Единица | Источник |
|---------|---------|---------|
| Throughput (RPS) | req/sec | Счётчик завершённых запросов / секунда |
| Throughput (bandwidth) | MB/s | Суммарный объём данных / секунда |
| Throughput (packets) | Mpps | Мегапакеты в секунду |
| Latency p50 | us | HdrHistogram |
| Latency p90 | us | HdrHistogram |
| Latency p99 | us | HdrHistogram |
| Latency p99.9 | us | HdrHistogram |
| Latency min/max/mean | us | HdrHistogram |
| Errors | count | Счётчик ошибок соединения |

### Системные метрики (от коллектора)

| Метрика | Единица | Источник |
|---------|---------|---------|
| CPU user % | % | `/proc/{pid}/stat` |
| CPU sys % | % | `/proc/{pid}/stat` |
| Voluntary context switches | count | `/proc/{pid}/status` |
| Involuntary context switches | count | `/proc/{pid}/status` |
| RSS memory | KB | `/proc/{pid}/status` (VmRSS) |
| VSZ memory | KB | `/proc/{pid}/status` (VmSize) |
| File descriptors | count | `/proc/{pid}/fd` (directory listing) |
| Syscalls distribution | count | `strace -c -S calls` (summary по типам) |
| Kernel scheduling delays | ms | `perf sched record` + `perf sched latency` |

Все метрики (кроме syscalls и perf) собираются **посекундно** для визуализации изменений во время теста.

### Особенности сбора метрик для FFM-моделей

| Аспект | Netty-модели (JNI) | FFM (однопоточный) | FFM-MT (многопоточный) |
|--------|-------------------|------------|----------------------|
| Throughput | Стабильный на протяжении 30 сек | При 1 conn стабильный; при >10 conn падает до 0 | Стабильный при всех conn |
| Memory (RSS) | Всегда доступен | server_rss_kb = 0 при >1 conn (сервер умирает раньше collector'а) | Всегда доступен |
| Strace | Только `futex` видим (JNI скрывает I/O syscalls) | Отключён (ptrace-конфликт с Panama FFM) | Отключён (ptrace-конфликт с Panama FFM) |
| FD count | Корректный (PID через `ss -tlnp`) | Корректный (PID через `ss -tlnp`) | Корректный (PID через `ss -tlnp`) |
| SQPOLL threads | N/A | N/A | Видны в /proc как kernel threads |
| Обработка нулей | Не требуется | Нулевые throughput и RSS отфильтрованы | Не требуется |

## 8. Формат результатов

Каждый тест сохраняет результаты в отдельную директорию:
```
results/{model}_{cpu}c_{conns}conn_{size}_run{N}/
├── throughput.csv          # per-second: RPS, MB/s, Mpps
├── latency.csv             # per-second: p50, p90, p99, p999
├── cpu.csv                 # per-second: server/client user/sys %
├── context_switches.csv    # per-second: voluntary/involuntary
├── memory.csv              # per-second: RSS, VSZ
├── fd_count.csv            # per-second: open file descriptors
├── syscalls.csv            # summary: syscall breakdown by type
├── perf_sched_latency.txt  # summary: kernel scheduling delays
└── strace_raw.txt          # raw strace output (FFM и FFM-MT)
```

Для Netty-моделей дополнительно сохраняется `strace_raw.txt` — вывод `strace -f -c`. Для FFM и FFM-MT моделей strace отключён (ptrace-конфликт с Panama FFM).

### Дополнительные результаты (FD-fix валидация)

```
results_fd_fix/{model}_4c_{conns}conn_4096_run1/
```

30 тестов (6 моделей x 5 conn) с исправленным FD collector. Используются в notebook v4.3 для корректной секции File Descriptors и сравнения с ошибочными данными из основного прогона.

## 9. Анализ и визуализация

### Jupyter notebook (`benchmark_analysis_v4.3.ipynb`)

Графики (50 ячеек, 39 PNG):
1. **Throughput vs Connections** — для каждого размера данных и CPU конфигурации (6 моделей)
2. **Throughput vs Data Size** — влияние размера payload на RPS
3. **Time-series** — изменение каждой метрики по секундам во время одного теста, все 6 моделей на одном графике
4. **Latency distribution** — boxplot p99 для каждой модели
5. **CPU utilization** — grouped bar (user + sys) для каждой модели при разной нагрузке
6. **Context switches** — voluntary переключения контекста
7. **Syscall breakdown** — анализ strace данных + сравнение FFM vs JNI visibility
7b. **FFM strace analysis** — детальный разбор syscalls FFM и FFM-MT моделей
8. **Memory usage** — RSS growth с увеличением соединений
9. **File Descriptors (корректные данные)** — из `results_fd_fix/`, реальные FD сервера
9.1. **FD-fix валидация** — сравнение старых (Gradle PID) и новых (Server PID) FD-данных
10. **Scalability** — throughput при 1/4/8 ядрах
11. **Summary tables** — сводные таблицы результатов
12. **FFM Deep Dive** — все 6 моделей при 1 conn (честное сравнение)
13. **FFM-MT vs JNI deep dive** — прямое сравнение при всех conn + ratio chart
14. **All 6 models** — сравнение при 1000 conn (throughput/latency/memory)

### Обработка данных FFM

При анализе данных однопоточного FFM применяются специальные фильтры:

- **Throughput**: `df[df['throughput_rps'] > 0]` — исключение нулевых секунд (когда сервер повис из-за ring overflow). Применяется только при вычислении средних, не для time-series.
- **Memory**: `df[df['server_rss_kb'] > 0]` — исключение нулевых значений (сервер умер раньше, чем collector снял данные).
- **Time-series**: отображаются все данные без фильтрации — чтобы показать паттерн зависания FFM-сервера.

Для FFM-MT фильтрация нулей не ожидается — многопоточная архитектура должна оставаться стабильной при всех конфигурациях.

### Ожидаемые наблюдения
- **Blocking**: линейный рост потоков и context switches при высоких connections; высокое потребление памяти (стек на каждый поток)
- **NIO**: стабильный CPU при умеренных connections; overhead от Java NIO Selector абстракции
- **Epoll**: аналогично NIO, но меньше overhead — прямой доступ к epoll через JNI
- **io_uring (JNI)**: значительно меньше syscalls (batching SQE/CQE); потенциально лучше при высоких connections; ниже kernel overhead
- **io_uring (FFM)**: стабильный throughput ~17K RPS при 1 conn; ring overflow и зависание при >10 conn; полный профиль syscalls в strace
- **io_uring (FFM-MT)**: сопоставимый throughput с JNI благодаря одинаковой потоковой модели; полный профиль syscalls; потенциальный выигрыш от SQPOLL и fixed buffers

## 10. Сценарий воспроизведения

### Подготовка

```bash
# 1. Установить зависимости
/ssd/benchmark/scripts/setup_environment.sh

# 2. Переключить JDK на версию 21
update-alternatives --set java /usr/lib/jvm/java-21-openjdk-amd64/bin/java
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64

# 3. Сгенерировать Gradle wrapper
cd /ssd/benchmark
gradle wrapper --gradle-version 8.12

# 4. Собрать проект
./gradlew build -x test

# 5. Применить kernel tuning
sysctl -w net.core.somaxconn=65535
sysctl -w net.ipv4.tcp_max_syn_backlog=65535
sysctl -w "net.ipv4.ip_local_port_range=1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1
ulimit -n 1048576
```

### Запуск одного теста

```bash
# Формат: run_single_test.sh <model> <port> <connections> <data_size> <cpu_config> <run_number>
/ssd/benchmark/scripts/run_single_test.sh iouring-ffm-mt 8080 1000 4096 4c 1
```

### Запуск полной матрицы

```bash
# Все 1440 тестов (6 моделей) в фоне
nohup /ssd/benchmark/scripts/run_all_benchmarks.sh 8080 > /ssd/benchmark/benchmark.log 2>&1 &

# Мониторинг прогресса
tail -f /ssd/benchmark/benchmark.log
```

### Запуск FFM-MT

```bash
# HTTP-сервер FFM-MT
./gradlew :servers:iouring-ffm-mt:run --args="8080 4"

# Бенчмарк FFM-MT (отдельный набор из 240 тестов)
/ssd/benchmark/scripts/run_ffm_mt_benchmarks.sh 8080
```

### Анализ результатов

```bash
# Генерация notebook
python3 /ssd/benchmark/reports/generate_notebook_v4.3.py

# Выполнение notebook
cd /ssd/benchmark/reports
jupyter nbconvert --to notebook --execute benchmark_analysis_v4.3.ipynb --output benchmark_analysis_v4.3_executed.ipynb
```

## 11. Ограничения

- Тестирование на loopback (не реальная сеть) — нет network latency
- Короткоживущие соединения — не покрывает сценарий keepalive/websocket
- Один клиент — нет distributed нагрузки
- Фиксированный размер данных в каждом тесте — нет смешанной нагрузки
- JIT warmup за 5 секунд может быть недостаточен для некоторых сценариев
- strace добавляет overhead к измеряемому серверу (syscalls.csv собирается параллельно)
- perf sched требует root-прав и может влиять на результаты при высокой нагрузке
- **FFM однопоточный** — нельзя напрямую сравнивать throughput FFM и Netty-моделей при >1 conn
- **FFM ring overflow** — при >10 conn FFM-сервер зависает; данные throughput после зависания = 0 (отфильтрованы при агрегации)
- **FFM memory** — server_rss_kb = 0 при многих конфигурациях (сервер умирает раньше collector'а)
- **FFM-MT SQPOLL** — kernel SQPOLL thread потребляет CPU даже в idle (timeout 1000ms); при бенчмарке этот overhead незначителен, но заметен в idle-метриках
- **FFM-MT multishot accept** — отключен из-за EINVAL на ядре 6.14; используется single-shot с re-arm (незначительный overhead)
- **Strace для JNI** — attach к запущенному JVM не захватывает I/O syscalls из нативных библиотек Netty
- **Strace для FFM** — отключён для FFM и FFM-MT моделей (ptrace-конфликт с Panama FFM API)
- **FD в основном прогоне** — данные fd_count.csv в `results/` некорректны (считались для Gradle daemon PID). Корректные данные в `results_fd_fix/` (30 тестов)

## 12. Воспроизводимость

- Фиксированный random seed (42) для генерации данных
- Все параметры задаются через аргументы командной строки
- Скрипты автоматизируют полный цикл тестирования
- CSV-формат результатов для независимого анализа
- Конфигурация машины автоматически фиксируется в Jupyter notebook

## 13. Исходный код модулей

| Модуль | Путь | Описание |
|--------|------|----------|
| servers/blocking | `servers/blocking/` | Netty OIO (thread-per-connection) |
| servers/nio | `servers/nio/` | Netty NIO transport |
| servers/epoll | `servers/epoll/` | Netty native epoll transport |
| servers/iouring | `servers/iouring/` | Netty io_uring transport (JNI) |
| servers/iouring-ffm-demo | `servers/iouring-ffm-demo/` | io_uring FFM (однопоточный) |
| servers/iouring-ffm-mt | `servers/iouring-ffm-mt/` | io_uring FFM (многопоточный, SQPOLL) |
| client | `client/` | BenchmarkClient (Virtual Threads, HdrHistogram) |

### Файлы модуля iouring-ffm-mt

```
servers/iouring-ffm-mt/
├── build.gradle.kts
└── src/main/java/benchmark/server/iouring/ffm/mt/
    ├── IoUringFfmMtServer.java   # Main: acceptor, response templates
    ├── IoUringNative.java        # FFM syscall bindings (io_uring, socket, mmap)
    ├── IoUringRing.java          # Ring abstraction (setup, mmap, getSqe, submit, SQPOLL)
    └── WorkerThread.java         # Worker event loop (recv/send/close, fixed buffers)
```
