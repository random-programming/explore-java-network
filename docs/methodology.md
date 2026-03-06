# Методология бенчмарка I/O моделей в Linux/Java

## 1. Цель исследования

Сравнительный анализ производительности четырёх моделей сетевого ввода-вывода в Linux на платформе JDK 21:

| Модель | Реализация | Механизм ядра |
|--------|-----------|---------------|
| Blocking I/O | Netty OIO (thread-per-connection) | `read()`/`write()` с блокировкой, поток блокируется до завершения операции |
| Non-blocking I/O | Netty NIO transport | Java NIO `Selector` → на Linux маппится на `epoll` через JDK-абстракцию |
| epoll (native) | Netty native epoll transport | Прямой `epoll_create`, `epoll_ctl`, `epoll_wait` через JNI, минуя Java NIO |
| io_uring | Netty io_uring transport (JNI) | `io_uring_setup`, `io_uring_enter` — асинхронный I/O с SQ/CQ кольцами |

Дополнительно: демонстрация io_uring через JDK 21 Foreign Function & Memory API (FFM) — без JNI.

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
| `/proc/{pid}/fd` | Файловые дескрипторы | Подсчёт файлов каждую секунду |
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
Все серверы идентичны по логике:
1. Принять соединение
2. Прочитать HTTP запрос
3. Определить запрошенный размер данных
4. Отправить pre-generated данные в HTTP ответе
5. Закрыть соединение

Данные генерируются один раз при старте сервера с фиксированным seed для воспроизводимости.

### Клиент
- Java `java.net.Socket` (raw TCP, не HttpClient)
- Виртуальные потоки (JDK 21) для генерации нагрузки
- Каждый виртуальный поток: цикл connect-send-recv-close
- Сбор latency через HdrHistogram (наносекундная точность, 3 significant digits)

## 5. Матрица параметров

### Независимые переменные

| Параметр | Значения |
|----------|---------|
| I/O модель | blocking, nio, epoll, iouring |
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
4 × 5 × 8 × 3 × 2 повтора = **960 тестов**

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
| Latency p50 | μs | HdrHistogram |
| Latency p90 | μs | HdrHistogram |
| Latency p99 | μs | HdrHistogram |
| Latency p99.9 | μs | HdrHistogram |
| Latency min/max/mean | μs | HdrHistogram |
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
└── perf_sched_latency.txt  # summary: kernel scheduling delays
```

## 9. Анализ и визуализация

### Jupyter notebook (`benchmark_analysis.ipynb`)

Графики:
1. **Throughput vs Connections** — для каждого размера данных и CPU конфигурации
2. **Time-series** — изменение каждой метрики по секундам во время одного теста, все модели на одном графике
3. **Latency distribution** — boxplot p50/p90/p99 для каждой модели
4. **CPU utilization** — user vs sys для каждой модели при разной нагрузке
5. **Context switches** — voluntary vs involuntary
6. **Syscall breakdown** — horizontal bar chart по типам syscalls
7. **Memory usage** — RSS growth с увеличением соединений
8. **FD count** — количество открытых дескрипторов
9. **Scalability** — throughput при 1/4/8 ядрах
10. **Summary table** — сводная таблица результатов

### Ожидаемые наблюдения
- **Blocking**: линейный рост потоков и context switches при высоких connections; высокое потребление памяти (стек на каждый поток)
- **NIO**: стабильный CPU при умеренных connections; overhead от Java NIO Selector абстракции
- **Epoll**: аналогично NIO, но меньше overhead — прямой доступ к epoll через JNI
- **io_uring**: значительно меньше syscalls (batching SQE/CQE); потенциально лучше при высоких connections; ниже kernel overhead

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
/ssd/benchmark/scripts/run_single_test.sh epoll 8080 1000 4096 4c 1
```

### Запуск полной матрицы

```bash
# Все 960 тестов в фоне
nohup /ssd/benchmark/scripts/run_all_benchmarks.sh 8080 > /ssd/benchmark/benchmark.log 2>&1 &

# Мониторинг прогресса
tail -f /ssd/benchmark/benchmark.log
```

### Запуск io_uring FFM демо

```bash
# HTTP-сервер на FFM io_uring
./gradlew :servers:iouring-ffm-demo:run --args="8080"

# Бенчмарк JNI vs FFM overhead
./gradlew :servers:iouring-ffm-demo:benchmark
```

### Анализ результатов

```bash
# Запуск Jupyter notebook
cd /ssd/benchmark/reports
jupyter notebook benchmark_analysis.ipynb
```

## 11. Ограничения

- Тестирование на loopback (не реальная сеть) — нет network latency
- Короткоживущие соединения — не покрывает сценарий keepalive/websocket
- Один клиент — нет distributed нагрузки
- Фиксированный размер данных в каждом тесте — нет смешанной нагрузки
- JIT warmup за 5 секунд может быть недостаточен для некоторых сценариев
- strace добавляет overhead к измеряемому серверу (syscalls.csv собирается параллельно)
- perf sched требует root-прав и может влиять на результаты при высокой нагрузке

## 12. Воспроизводимость

- Фиксированный random seed (42) для генерации данных
- Все параметры задаются через аргументы командной строки
- Скрипты автоматизируют полный цикл тестирования
- CSV-формат результатов для независимого анализа
- Конфигурация машины автоматически фиксируется в Jupyter notebook
