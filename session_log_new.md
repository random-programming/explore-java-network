# Session Log: FFM Benchmark Launch (2026-03-08)

## Цель
Провести тестирование io_uring FFM-сервера (240 тестов) для закрытия замечаний по отчёту:
- Замечание 1: Нет результатов FFM в отчёте
- Замечание 2: Все метрики io_uring — это Netty+JNI, а не FFM
- Замечание 4: Syscalls не показывают преимущества io_uring (strace не видит JNI)

## Матрица тестов FFM
- Модель: `iouring-ffm` (однопоточный, чистый JDK 21 FFM, без Netty)
- Connections: 1, 10, 100, 1000, 10000
- Data sizes: 64, 512, 4096, 16384, 65536, 131072, 524288, 1048576
- CPU configs: 1c, 4c, 8c
- Runs: 2
- **Итого: 5 × 8 × 3 × 2 = 240 тестов**
- Ожидаемое время: ~4-5 часов (средняя 58 сек/тест по опыту предыдущего прогона)

## Что было сделано в этой сессии

### 1. Подготовка скриптов
- Добавлена модель `iouring-ffm` в `run_single_test.sh` (маппинг на модуль `servers:iouring-ffm-demo`)
- Создан скрипт `scripts/run_ffm_benchmarks.sh` — аналог `run_all_benchmarks.sh`, но только для FFM
  - Connections: 1, 10, 100, 1000, 10000 (полная матрица)
  - Resume-поддержка (пропуск уже выполненных тестов)
  - Проверка свободного места на /ssd (< 20 ГБ → стоп)
  - Telegram-уведомления
  - `|| continue` при ошибке теста (не останавливать весь прогон)
  - Начальная задержка 10 сек

### 2. Проверка FFM-сервера
- Сборка: `gradlew :servers:iouring-ffm-demo:build` — успешно
- Тест: запуск на порту 9999, curl `http://localhost:9999/?size=64` → HTTP 200 — работает

### 3. Ошибочный запуск (без tmux)
- Случайно запустился тест не через tmux, а как background-процесс
- Во время теста claude (текущая сессия) занимал ~48% CPU
- Успели выполниться 2 теста: `iouring-ffm_1c_1conn_64_run1` и `iouring-ffm_1c_1conn_64_run2`
- **Результаты искажены** — claude конкурировал за CPU на том же процессоре (config=1c, CPU 0)
- Решение: удалить эти 2 результата и перезапустить с нуля

### 4. Очистка системы
- Убиты все оставшиеся процессы: FFM-сервер, Gradle daemon, client, collector
- Проверка ресурсов: 28.9 ГБ RAM free, 659 ГБ диск free, CPU 89% idle

## Запуск
- Удалить 2 некорректных результата
- Запуск через tmux (как в предыдущем прогоне):
```bash
tmux new-session -d -s ffm-bench 'bash -c "/ssd/benchmark/scripts/run_ffm_benchmarks.sh 8080 2>&1 | tee /ssd/benchmark/ffm_benchmark.log"'
```
- Подключение: `tmux attach -t ffm-bench`
- Можно отключиться сразу — tmux, tee и Telegram работают автономно

## Предыдущий прогон (справка)
- 960 тестов (blocking, nio, epoll, iouring-JNI) — все завершены
- Время: 934 мин (15.5 часов)
- Первый запуск: система легла (perf sched record -a, диск забился)
- Исправления: perf sched -p, trap cleanup, resume, disk check
- Запуск: `tmux new-session -d -s bench 'bash -c "run_all_benchmarks.sh 8080 2>&1 | tee benchmark.log"'`

## Риски для FFM с 10000 connections
- FFM-сервер однопоточный, ring 256 entries → будет очень медленный при 10000 conn
- Это НЕ ошибка — это корректный результат, показывающий ограничения
- Safety timeout 120 сек убьёт зависший тест
- Система не ляжет: все исправления perf sched на месте, 659 ГБ свободно

---

## Результаты FFM-прогона

### Статус: ЗАВЕРШЁН (240/240 тестов)

### Наблюдения по данным FFM
1. **Throughput**: при 1 conn — стабильные ~17K RPS; при >10 conn ring переполняется, throughput падает до 0 после нескольких секунд
2. **Syscalls**: strace_raw.txt у FFM содержит реальные syscalls (epoll_wait, read, write, mmap, pread64) — в отличие от JNI-моделей, где виден только futex
3. **Memory**: server_rss_kb = 0 при >1 conn (сервер умирает раньше collector'а); при 1 conn — ~100–111 MB
4. **Latency**: аномально высокий p99 при 4KB и 16KB (промежуточные размеры)

---

## Сессия: Создание отчёта v3 (2026-03-08)

### Что сделано

#### 1. Notebook v3 (`generate_notebook_v3.py` → `benchmark_analysis_v3.ipynb`)
- 5 моделей: blocking, nio, epoll, iouring (JNI), iouring-ffm
- 41 ячейка, 30 графиков, 8 сводных таблиц
- Regex обновлён: `iouring-ffm` перед `iouring` (иначе regex съедает начало)
- Фильтрация нулей: `throughput_rps > 0` при агрегации средних, `server_rss_kb > 0` для memory
- Новая секция 7.1: анализ strace_raw.txt FFM vs JNI (bar chart сравнение)
- Новая секция 12: FFM Deep Dive — честное сравнение всех 5 моделей при 1 conn (bar charts throughput + latency + сводные таблицы)
- Все x-axis labels обновлены: `['Block', 'NIO', 'Epoll', 'iou', 'FFM']`
- Исправлен парсер strace: убрана строка "total" из графика (не syscall, а сумма)
- `pd.to_numeric(errors='coerce')` для всех числовых столбцов (fix для mixed dtypes после concat)

#### 2. Методология v3 (`methodology_v3.md` → `methodology_v3.pdf`)
- 5 моделей в таблице, 1200 тестов
- Новый раздел: «Различие io_uring JNI и io_uring FFM» (потоковая модель, strace visibility, масштабируемость)
- Раздел «Особенности сбора метрик для FFM» (фильтрация нулей, strace visibility)
- Обновлены ограничения: FFM однопоточный, ring overflow, memory = 0
- 11 страниц PDF, русский текст, таблицы

#### 3. Git push
- Коммит: `3fbbc3c` на `main`
- 2039 файлов: 240 FFM результатов (CSV + strace_raw.txt + perf), v3 отчёты, методология, скрипты
- SSH push на `git@github.com:random-programming/explore-java-network.git`
- Старые файлы (v2, executed) не тронуты

### Файлы v3

| Файл | Описание |
|------|----------|
| `reports/generate_notebook_v3.py` | Генератор notebook (5 моделей) |
| `reports/benchmark_analysis_v3.ipynb` | Чистый notebook (без вывода) |
| `reports/benchmark_analysis_v3_executed.ipynb` | Выполненный notebook (8.5 MB, 30 графиков) |
| `docs/methodology_v3.md` | Методология (23 KB) |
| `docs/methodology_v3.pdf` | PDF методологии (71 KB, 11 стр.) |
| `docs/md_to_pdf_v3.py` | Конвертер MD → PDF |
| `results/iouring-ffm_*` | 240 директорий с результатами FFM |

### Замечания из v2 — статус закрытия

| # | Замечание | Статус | Как решено в v3 |
|---|-----------|--------|----------------|
| 1 | Нет FFM в отчёте | **Закрыто** | FFM на всех графиках как 5-я модель |
| 2 | io_uring метрики = Netty+JNI | **Закрыто** | FFM показывает чистый io_uring без Netty |
| 3 | FFM throughput ниже Netty | **Закрыто** | Объяснено однопоточностью; deep dive при 1 conn |
| 4 | Syscalls не показывают io_uring | **Частично** | FFM strace видит epoll_wait/read/write; io_uring_enter всё ещё не виден |
| 5 | Аномалии при промежуточных размерах | **Подтверждено** | FFM данные показывают тот же crossover pattern |

---

## Анализ: многопоточный FFM-сервер (2026-03-08)

### Проблема с текущим сравнением

Сравнение FFM vs Netty при conn > 1 некорректно: FFM-сервер однопоточный, Netty использует N потоков. Разница в throughput (2-4x) объясняется не преимуществом Netty/JNI, а разницей в параллелизме. Это **ограничение реализации**, а не технологии FFM.

### Почему текущий FFM-сервер однопоточный

Код (`IoUringFfmServer.java`) написан как учебная реализация ("single-threaded event loop for simplicity/clarity"). Архитектурные барьеры для многопоточности:
- `Arena.ofConfined()` — память доступна только из создавшего потока
- Один io_uring ring (256 entries) — переполняется при >10 conn
- `HashMap` для буферов соединений — не thread-safe
- Прямой доступ к SQ/CQ указателям без синхронизации

### Может ли FFM быть многопоточным?

**Да.** FFM API полностью поддерживает многопоточность. Архитектура:
1. **Main thread (acceptor)** — собственный io_uring ring, принимает соединения, распределяет fd по worker'ам (round-robin)
2. **N worker threads** — каждый со своим io_uring ring, своей `Arena.ofConfined()`, своими буферами
3. **Передача fd** от acceptor к worker — через `ConcurrentLinkedQueue<Integer>`
4. Каждый worker — полностью независимый event loop, без shared state

Это та же модель, которую использует Netty (`IOUringEventLoopGroup(threads)` — один ring на поток).

### Оценка рисков

| Риск | Оценка | Почему |
|------|--------|--------|
| Система ляжет | **Низкий** | Инфраструктура обкатана на 1200 тестах, timeout 120 сек, cleanup trap |
| Сервер зависнет при 10K conn | **Средний** | 10K / N workers = 2500 conn на worker, ring может переполниться. Решение: увеличить ring до 4096 |
| Баги многопоточности | **Средний** | Некорректная передача fd, утечки arena. Решение: ручное тестирование перед полным прогоном |
| Throughput = 0 при высоких conn | **Ожидаемо** | Корректный результат, wrk завершится по таймауту, тест пойдёт дальше |

### План

1. Написать многопоточный FFM-сервер (модель `iouring-ffm-mt`)
2. Ручная проверка: 1, 10, 100, 1000 conn
3. Полный прогон 240 тестов (~4-5 часов)
4. Обновить отчёт v4: честное сравнение FFM-MT vs Netty при всех conn

---

## Сессия: Реализация iouring-ffm-mt (2026-03-09)

### Что сделано

#### Создан модуль `servers/iouring-ffm-mt` — 5 файлов:

| Файл | Описание | Статус |
|------|----------|--------|
| `build.gradle.kts` | Gradle конфигурация, MainClass = IoUringFfmMtServer | Готов |
| `IoUringNative.java` | FFM-биндинги io_uring (копия demo + multishot/fixed buffer helpers) | Готов |
| `IoUringRing.java` | Абстракция одного io_uring ring (setup+mmap, getSqe, submit, forEachCqe, registerBuffers) | Готов |
| `WorkerThread.java` | Worker: свой ring, fixed buffers (2048x4KB), recv→send→close, keep-alive | Готов |
| `IoUringFfmMtServer.java` | Main: pre-built responses в Arena.global(), acceptor loop, round-robin | Готов |

#### Обновлены существующие файлы:
- `settings.gradle.kts` — добавлен `include("servers:iouring-ffm-mt")`
- `scripts/run_single_test.sh` — добавлен case `iouring-ffm-mt`

#### Архитектура (финальная):
```
Acceptor (main thread)         Worker 0..N-1 (каждый свой поток)
┌──────────────────┐          ┌──────────────────┐
│ io_uring ring    │  fd →    │ io_uring ring    │
│ (256 entries)    │ round-   │ (4096 entries)   │
│ single-shot      │ robin    │ fixed buffers    │
│ accept + re-arm  │ via CLQ  │ (2048 x 4KB)     │
└──────────────────┘          │ recv→send→close  │
                              └──────────────────┘
```

#### Оптимизации реализованные:
- Per-thread rings (4096 entries) — нулевая контенция
- Fixed buffers (REGISTER_BUFFERS) — нет page table lookup на recv
- Pre-built response templates в Arena.global() — zero per-request alloc на send
- CQ size = 2x SQ — защита от overflow
- Keep-alive поддержка (парсинг Connection header)
- Partial send handling (для больших ответов)

#### Что НЕ реализовано (решено отложить):
- **SQPOLL** — отключён. Причина: нестабильная работа (~15-20% потерянных соединений). Проблема в memory ordering между userspace tail-write и kernel SQPOLL thread. Выигрыш SQPOLL = ~5-15% на syscall overhead, не критично для бенчмарка.
- **Multishot accept** — отключён. Ядро 6.14 возвращает EINVAL (-22). Используется single-shot accept с re-arm (как в demo).
- **SEND_ZC (zero-copy send)** — не реализовано, может дать прирост для больших ответов
- **Registered files (REGISTER_FILES)** — не реализовано, может дать прирост при 10K+ fd

### Компиляция
- `./gradlew :servers:iouring-ffm-mt:build` — **УСПЕШНО**

### Тестирование — текущие проблемы

**Основная проблема: ~15-20% запросов не получают ответ (curl timeout, HTTP 000)**

Проблема воспроизводится И с SQPOLL, И без SQPOLL. Фейлы случайные, не привязаны к конкретному worker'у или размеру данных.

#### Что точно работает:
- Сервер стартует, все 8 workers инициализируются, fixed buffers регистрируются
- Accept соединений работает (acceptor ring)
- recv/send/close pipeline работает (правильные HTTP ответы, правильные размеры)
- Все 8 размеров данных отдаются корректно (64B — 1MB)
- Headers Connection: close и Connection: keep-alive генерируются правильно
- Partial send для больших ответов работает

#### Что не работает:
- ~15-20% запросов при быстрой последовательной отправке не получают ответ
- `println` debug не виден через gradle (gradle перехватывает stdout)
- Без debug трассировки невозможно определить точное место зависания

### Следующие шаги (для следующей сессии)

1. **Найти и исправить баг с потерянными соединениями:**
   - Запустить сервер через `java` напрямую (не через gradle) для debug вывода
   - Добавить debug prints в drainNewFds, handleRecv, handleSend, handleClose
   - Определить: fd доходит до worker? recv SQE отправляется? CQE приходит?
   - Возможные причины:
     - Race в acceptor: re-arm accept внутри forEachCqe при нескольких CQE одновременно
     - Worker спит на Thread.sleep(1) когда connections пуст, и пропускает fd
     - getSqe() в acceptor ring возвращает null (ring полон из-за re-arm)

2. **После исправления — прогнать тесты:**
   ```bash
   # Smoke test
   curl -v "http://localhost:9999/?size=64"

   # 30 sequential requests — 100% pass rate
   for i in $(seq 1 30); do curl -s --max-time 3 -o /dev/null -w "%{http_code}\n" "http://localhost:9999/?size=4096"; done

   # Client тест (1, 100, 1000, 10000 connections)
   ./gradlew :client:run --args="localhost 9999 100 4096 2 10 /tmp/test_mt"
   ```

3. **Вернуть SQPOLL после исправления бага:**
   - Проблема SQPOLL была в memory ordering: userspace пишет SQ tail, SQPOLL kernel thread на другом CPU не видит обновление
   - Попытки починить через `VarHandle.storeStoreFence()` + `fullFence()` не помогли полностью
   - Но основной баг (~15-20% потерь) воспроизводится и БЕЗ SQPOLL — значит причина в другом месте
   - После исправления основного бага: вернуть `IoUringRing.create(arena, entries, true, sqCpu, sqIdleMs)` в WorkerThread
   - Код SQPOLL уже есть в IoUringNative.java (IORING_SQ_NEED_WAKEUP, IORING_ENTER_SQ_WAKEUP, ioUringEnterRaw)
   - IoUringRing.java нужно будет вернуть: поле sqpollEnabled, sqFlagsOff, pendingTail, submit() с SQPOLL веткой
   - Рабочая версия IoUringRing с SQPOLL сохранена в git истории (не коммичена, но есть в claude transcript)

4. **Полный прогон 240 тестов через tmux**

### Файлы модуля

```
/ssd/benchmark/servers/iouring-ffm-mt/
├── build.gradle.kts
└── src/main/java/benchmark/server/iouring/ffm/mt/
    ├── IoUringFfmMtServer.java   (main, acceptor, response templates)
    ├── IoUringNative.java        (FFM syscall bindings)
    ├── IoUringRing.java          (ring abstraction)
    └── WorkerThread.java         (worker event loop)
```
