# План: Бенчмарк моделей I/O в Linux/Java

## 1. Обзор проекта

Сравнительное тестирование 4 моделей сетевого I/O на JDK 21:
- **Blocking I/O** — классический подход, поток на соединение (Tomcat / Netty с OIO)
- **Non-blocking I/O** — Java NIO (Selector-based), Netty NIO transport
- **I/O Multiplexing (epoll)** — Netty native epoll transport; select опционально (показать ограничения)
- **io_uring** — Netty native io_uring transport (JNI) + собственная FFM-демонстрация

## 2. Структура проекта

```
/ssd/benchmark/
├── build.gradle.kts              # Gradle multi-module build
├── settings.gradle.kts
├── servers/
│   ├── blocking-server/          # Blocking I/O HTTP server
│   ├── nio-server/               # Non-blocking NIO server
│   ├── epoll-server/             # Epoll native server
│   ├── iouring-server/           # io_uring Netty server (JNI)
│   └── iouring-ffm-demo/         # io_uring FFM демонстрация
├── client/                       # Единый Java HTTP-клиент для нагрузки
├── collector/                    # Сборщик системных метрик
├── scripts/
│   ├── run_all_benchmarks.sh     # Главный скрипт запуска всех тестов
│   ├── run_single_test.sh        # Запуск одной конфигурации
│   ├── collect_metrics.sh        # Сбор метрик во время теста
│   └── setup_environment.sh      # Установка зависимостей
├── results/                      # CSV-файлы результатов
│   └── {model}_{cores}c_{conns}conn_{size}/
│       ├── throughput.csv
│       ├── latency.csv
│       ├── cpu.csv
│       ├── syscalls.csv
│       ├── context_switches.csv
│       ├── memory.csv
│       └── fd_count.csv
├── reports/
│   └── benchmark_analysis.ipynb  # Jupyter notebook с визуализацией
└── docs/
    └── methodology.md            # Методология испытаний
```

## 3. Технологический стек

### Серверная часть
| Модель | Реализация | Детали |
|--------|-----------|--------|
| Blocking | Netty OIO transport | Bounded ThreadPool (потоков = connections * 2, max 2000) |
| Non-blocking | Netty NIO transport | Fixed EventLoopGroup (потоков = CPU cores) |
| Epoll | Netty native epoll | Fixed EventLoopGroup (потоков = CPU cores) |
| io_uring (JNI) | Netty io_uring transport | `netty-transport-native-io_uring` |
| io_uring (FFM) | Свой код на JDK 21 FFM | Демо: ring setup, SQE/CQE, fixed buffers, multishot accept |

### Клиентская часть
- Свой Java HTTP-клиент на `java.net.Socket` (raw TCP)
- Каждый запрос = новое TCP-соединение (короткоживущие)
- Управляемый пул виртуальных потоков для генерации нагрузки
- Встроенный сбор latency (наносекундная точность, HdrHistogram)

### Инструменты сбора метрик
| Метрика | Инструмент | Способ |
|---------|-----------|--------|
| CPU usage | `mpstat`, `pidstat` | Запись каждую секунду |
| Context switches | `pidstat -w` | vol/invol отдельно, каждую секунду |
| Syscalls | `strace -c -S calls` | Разбивка по типам (summary) |
| Syscalls timeline | `strace -T -e trace=network,desc` | Выборочно, для анализа |
| Kernel delays | `perf sched record` + `perf sched latency` | Базовые метрики |
| Memory (RSS) | `/proc/{pid}/status`, `/proc/{pid}/smaps_rollup` | Каждую секунду |
| File descriptors | `/proc/{pid}/fd` (count) | Каждую секунду |
| Throughput/Latency | Встроенные метрики клиента | HdrHistogram, p50/p90/p99/p999 |

### Сборка и окружение
- **JDK**: OpenJDK 21 (установить через apt)
- **Build**: Gradle 8.x с Kotlin DSL
- **Python**: 3.12 + jupyter, matplotlib, pandas, seaborn (для отчётов)
- **Дополнительно**: liburing-dev (для FFM-демо)

## 4. Параметры нагрузки

### Матрица тестов
- **Соединения**: 1, 10, 100, 1 000, 10 000
- **Размер данных**: 64B, 512B, 4KB, 16KB, 64KB, 128KB, 512KB, 1MB
- **CPU конфигурации**:
  - 1 ядро: сервер + клиент на CPU 0 (`taskset -c 0`)
  - 4 ядра: сервер на CPU 0,1; клиент на CPU 2,3 (`taskset -c 0,1` / `taskset -c 2,3`)
  - 8 ядер: сервер на CPU 0-3; клиент на CPU 4-7 (`taskset -c 0-3` / `taskset -c 4-7`)
- **HyperThreading**: отключен через выбор только физических ядер (0-7, без 8-15)

### Протокол теста
1. Запуск сервера с CPU pinning
2. Warmup: 5 секунд
3. Активное измерение: 30 секунд
4. Сбор метрик: параллельно в фоне каждую секунду
5. Остановка, сохранение результатов
6. Повтор: 2 раза для каждой конфигурации
7. Пауза между тестами: 5 секунд (дать ОС стабилизироваться)

### Общее количество конфигураций
- 4 модели x 5 уровней соединений x 8 размеров x 3 CPU конф. x 2 повтора = **960 тестов**
- Длительность одного теста: ~40 секунд (5 warmup + 30 active + 5 pause)
- Ориентировочно: ~640 минут (~11 часов) на все тесты
- FFM-демо тестируется отдельно с подмножеством конфигураций

## 5. HTTP протокол

### Запрос
```
GET /data?size={bytes} HTTP/1.1
Host: localhost
Connection: close
```

### Ответ
```
HTTP/1.1 200 OK
Content-Length: {bytes}
Content-Type: application/octet-stream
Connection: close

{random bytes}
```

- `Connection: close` — гарантирует короткоживущие соединения
- Данные генерируются заранее в буфер (не тратим CPU на генерацию в рантайме)

## 6. Формат CSV

Отдельный файл на каждую конфигурацию. Имя директории: `{model}_{cores}c_{conns}conn_{size}_run{N}`

### throughput.csv
```
timestamp_sec,requests_completed,bytes_sent,throughput_rps,throughput_mbps
1,245,1003520,245,7.82
2,512,2097152,267,8.53
...
```

### latency.csv
```
timestamp_sec,p50_us,p90_us,p99_us,p999_us,min_us,max_us,mean_us,stddev_us
1,120,350,980,2100,45,3200,185,142
...
```

### cpu.csv
```
timestamp_sec,server_user_pct,server_sys_pct,server_total_pct,client_user_pct,client_sys_pct,client_total_pct
1,12.5,8.3,20.8,15.2,10.1,25.3
...
```

### syscalls.csv
```
syscall_name,count,errors,total_time_us,avg_time_us,pct_time
epoll_wait,15234,0,45230,2.97,18.2
write,12456,0,38920,3.12,15.7
read,12456,12,36100,2.90,14.5
...
```

### context_switches.csv
```
timestamp_sec,server_voluntary,server_involuntary,client_voluntary,client_involuntary
1,234,12,189,8
...
```

### memory.csv
```
timestamp_sec,server_rss_kb,server_vsz_kb,server_heap_kb,client_rss_kb
1,125432,2048576,98304,89216
...
```

### fd_count.csv
```
timestamp_sec,server_fd_count,client_fd_count
1,156,112
...
```

## 7. io_uring FFM-демо

Отдельный модуль, демонстрирующий:

### Что показываем
1. **Прямой вызов io_uring через FFM** (без JNI)
   - `io_uring_setup()`, `io_uring_enter()` через `Linker.nativeLinker()`
   - Работа с `MemorySegment` для SQE/CQE
2. **Расширенные возможности**:
   - Fixed buffers (`IORING_REGISTER_BUFFERS`)
   - Multishot accept (`IORING_ACCEPT_MULTISHOT`)
   - SQ Polling (`IORING_SETUP_SQPOLL`)
3. **Сравнение JNI vs FFM**:
   - Overhead вызова (наносекунды)
   - Удобство кода
   - Безопасность (FFM: confined arenas, автоматическое управление памятью)

### Преимущества FFM (для отчёта)
- Нет необходимости в нативном коде (C/C++)
- Нет JNI overhead (boundary crossing)
- Type-safe API
- Автоматическое управление off-heap памятью через Arena

### Недостатки FFM (для отчёта)
- Более verbose код по сравнению с обёрткой в JNI
- Необходимость ручного маппинга структур ядра
- Менее зрелая экосистема

## 8. Select: демонстрация ограничений

Опциональный модуль:
- Java NIO Selector (на Linux маппится на epoll, но мы покажем ограничения абстракции)
- Ключевое ограничение: `FD_SETSIZE = 1024` в POSIX select
- Демонстрация: при > 1024 соединениях select не работает, Java NIO Selector переключается на epoll
- В отчёте: таблица сравнения select vs epoll vs io_uring по ограничениям

## 9. План реализации (порядок работ)

### Этап 1: Подготовка окружения — DONE
1. [x] Установить JDK 21, Gradle, Jupyter, matplotlib, pandas, seaborn, liburing-dev
2. [x] Создать структуру Gradle-проекта
   - `build.gradle.kts` — корневой файл сборки
   - `settings.gradle.kts` — определение подпроектов
3. [x] Настроить зависимости (Netty 4.1.x, HdrHistogram, io_uring transport)

**Созданные файлы:**
- `/ssd/benchmark/build.gradle.kts`
- `/ssd/benchmark/settings.gradle.kts`

### Этап 2: Серверы — DONE
4. [x] Реализовать базовый HTTP handler (общий паттерн для всех моделей)
5. [x] Blocking server (raw ServerSocket + bounded thread pool)
6. [x] NIO server (Netty NIO transport)
7. [x] Epoll server (Netty native epoll transport)
8. [x] io_uring server (Netty native io_uring transport)

**Созданные файлы:**
- `/ssd/benchmark/servers/blocking-server/build.gradle.kts`
- `/ssd/benchmark/servers/blocking-server/src/main/java/benchmark/server/blocking/BlockingServer.java`
- `/ssd/benchmark/servers/nio-server/build.gradle.kts`
- `/ssd/benchmark/servers/nio-server/src/main/java/benchmark/server/nio/NioServer.java`
- `/ssd/benchmark/servers/epoll-server/build.gradle.kts`
- `/ssd/benchmark/servers/epoll-server/src/main/java/benchmark/server/epoll/EpollServer.java`
- `/ssd/benchmark/servers/iouring-server/build.gradle.kts`
- `/ssd/benchmark/servers/iouring-server/src/main/java/benchmark/server/iouring/IoUringServer.java`
- `/ssd/benchmark/servers/iouring-ffm-demo/build.gradle.kts` (только build-файл, реализации нет)
- `/ssd/benchmark/client/build.gradle.kts` (только build-файл, реализации нет)
- `/ssd/benchmark/collector/build.gradle.kts` (только build-файл, реализации нет)

### Этап 3: Клиент и коллектор — DONE
9. [x] HTTP-клиент нагрузки (raw sockets, виртуальные потоки, HdrHistogram)
10. [x] Коллектор системных метрик (pidstat, /proc, strace wrapper)

**Созданные файлы:**
- `/ssd/benchmark/client/src/main/java/benchmark/client/BenchmarkClient.java`
- `/ssd/benchmark/collector/src/main/java/benchmark/collector/MetricsCollector.java`

### Этап 4: Скрипты — DONE
11. [x] `setup_environment.sh` — установка всего необходимого
12. [x] `run_single_test.sh` — запуск одного теста с метриками
13. [x] `run_all_benchmarks.sh` — полный прогон матрицы тестов
14. [x] `collect_metrics.sh` — фоновый сбор метрик

**Созданные файлы:**
- `/ssd/benchmark/scripts/setup_environment.sh`
- `/ssd/benchmark/scripts/run_single_test.sh`
- `/ssd/benchmark/scripts/run_all_benchmarks.sh`
- `/ssd/benchmark/scripts/collect_metrics.sh`

### Этап 5: io_uring FFM-демо — DONE
15. [x] FFM-обёртка для io_uring syscalls
16. [x] Простой HTTP-сервер на FFM io_uring
17. [x] Бенчмарк JNI vs FFM overhead

**Созданные файлы:**
- `/ssd/benchmark/servers/iouring-ffm-demo/src/main/java/benchmark/server/iouring/ffm/IoUringNative.java`
- `/ssd/benchmark/servers/iouring-ffm-demo/src/main/java/benchmark/server/iouring/ffm/IoUringFfmServer.java`
- `/ssd/benchmark/servers/iouring-ffm-demo/src/main/java/benchmark/server/iouring/ffm/JniVsFfmBenchmark.java`
- `/ssd/benchmark/servers/iouring-ffm-demo/build.gradle.kts` (обновлён)

### Этап 6: Запуск и анализ — IN PROGRESS
18. [ ] Прогон всех тестов (запуск через tmux + tee для надёжности и логирования)
19. [x] Jupyter notebook с визуализацией (+ time-series графики, конфигурация машины)
20. [x] Документация методологии (methodology.md)
21. [x] Документация задания (assignment.md)

**Созданные файлы:**
- `/ssd/benchmark/reports/benchmark_analysis.ipynb`
- `/ssd/benchmark/docs/methodology.md`
- `/ssd/benchmark/docs/assignment.md`

### Доработки по ревью задания (все выполнены)
- [x] Throughput: MB/s + Mpps (было Mbit/s)
- [x] perf sched record + perf sched latency (задержки ядра)
- [x] Time-series графики (per-second изменение метрик во время теста)
- [x] Последовательность команд для воспроизведения (в methodology.md, раздел 10)
- [x] Конфигурация машины (автоматически в Jupyter notebook)
- [x] Анализ ограничений select/poll (в methodology.md, раздел 1)
- [x] Пояснение различия NIO vs Epoll (в methodology.md, раздел 1)

### Исправление: perf sched (2026-03-05)

**Проблема:** первый прогон (130/960 тестов) привёл к зависанию системы и принудительной перезагрузке.

**Причина:** `perf sched record -a` (system-wide) генерировал 2–3.8 ГБ данных за каждый 30-секундный тест. Парсинг (`perf sched latency`) не укладывался в таймаут collector'а (30 сек), скрипт убивался до выполнения `rm -f perf_sched.data`. За 130 тестов накопилось ~350 ГБ необработанных файлов, диск заполнился, система зависла.

**Исправления в `collect_metrics.sh`:**
1. `perf sched record -p <JAVA_PID>` вместо `-a` — запись только процесса сервера, размер данных на порядки меньше
2. `trap cleanup_perf EXIT` — гарантированное удаление `perf_sched.data` при любом завершении скрипта (включая kill)
3. `run_all_benchmarks.sh` — добавлена поддержка resume: скрипт проверяет наличие `throughput.csv` и `latency.csv` в директории результатов и пропускает уже завершённые тесты
4. Запуск через `tmux` + `tee` вместо `nohup` — live-мониторинг, лог на диске, возможность подключиться в любой момент
5. Проверка свободного места на `/ssd` между тестами — если менее 20 ГБ, скрипт останавливается и шлёт уведомление в Telegram. Проверка выполняется до запуска теста, не влияет на измерения
6. Задержка 40 секунд перед началом тестов — время для отключения клиента

**Команда запуска:**
```bash
tmux new-session -d -s bench 'bash -c "/ssd/benchmark/scripts/run_all_benchmarks.sh 8080 2>&1 | tee /ssd/benchmark/benchmark.log"'
```
Подключение: `tmux attach -t bench`

## 10. Верификация

- Перед полным прогоном: smoke-test каждого сервера (1 соединение, 4KB, 4 ядра)
- Проверка корректности HTTP ответов (Content-Length, статус 200)
- Проверка что CSV файлы заполняются корректно
- Сравнение результатов 2 повторов на отклонение (< 10% считаем стабильным)
