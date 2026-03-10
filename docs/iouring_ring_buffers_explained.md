# Кольцевые буферы io_uring: как сервер получает запросы и отправляет ответы

## 1. Общая идея: io_uring — это очередь команд, а не очередь данных

Распространённое заблуждение: «есть кольцо для входящих данных и кольцо для исходящих».
На самом деле io_uring устроен иначе.

Каждый io_uring instance содержит **два кольцевых буфера**:

| Буфер | Направление | Что содержит |
|-------|-------------|--------------|
| **SQ** (Submission Queue) | Приложение → Ядро | **Команды**: «прочитай из сокета», «запиши в сокет», «закрой fd» |
| **CQ** (Completion Queue) | Ядро → Приложение | **Результаты**: «recv завершён, 512 байт», «send завершён», «ошибка» |

И recv (чтение запроса клиента), и send (отправка ответа) — это просто **разные типы команд**, которые проходят через одну и ту же пару SQ/CQ.

## 2. Три mmap-региона в памяти

При создании io_uring instance ядро возвращает file descriptor, через который приложение делает `mmap` трёх отдельных регионов разделяемой памяти:

```
┌──────────────────────────────────────────────────────────────┐
│  mmap #1: SQ Ring                                            │
│  ┌──────┬──────┬──────┬─────────────────────────────────┐    │
│  │ head │ tail │ flags│ array[] — индексы в SQE array    │    │
│  └──────┴──────┴──────┴─────────────────────────────────┘    │
│                                                              │
│  mmap #2: SQE Array (массив 64-байтных структур)             │
│  ┌─────────┬─────────┬─────────┬─────────┬─────────┐        │
│  │ SQE[0]  │ SQE[1]  │ SQE[2]  │ SQE[3]  │   ...   │        │
│  │ 64 байт │ 64 байт │ 64 байт │ 64 байт │         │        │
│  └─────────┴─────────┴─────────┴─────────┴─────────┘        │
│                                                              │
│  mmap #3: CQ Ring (включая CQE массив inline)               │
│  ┌──────┬──────┬────────────────────────────────────┐        │
│  │ head │ tail │ CQE[0] CQE[1] CQE[2] CQE[3] ...  │        │
│  │      │      │ (16 байт каждый)                   │        │
│  └──────┴──────┴────────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

Важный нюанс: **SQ ring содержит не сами команды, а индексы** в отдельный SQE array. Это позволяет приложению заполнять SQE в произвольном порядке, а в SQ ring выстраивать их в нужной последовательности. В CQ ring, наоборот, результаты (CQE) лежат прямо внутри.

В коде сервера три `mmap` вызова видны в `IoUringRing.java`:

```java
// mmap #1: SQ ring (head, tail, flags, array)
MemorySegment sqRing = mmap(sqRingSize, ..., IORING_OFF_SQ_RING);

// mmap #2: CQ ring (head, tail, CQE entries inline)
MemorySegment cqRing = mmap(cqRingSize, ..., IORING_OFF_CQ_RING);

// mmap #3: SQE array (64-байтные структуры команд)
MemorySegment sqes = mmap(sqEntries * SQE_SIZE, ..., IORING_OFF_SQES);
```

## 3. Структура SQE (команда ядру) — 64 байта

Каждая команда ядру — это структура SQE (Submission Queue Entry):

| Поле | Смещение | Размер | Назначение |
|------|----------|--------|------------|
| `opcode` | 0 | 1 байт | Тип операции: RECV (27), SEND (26), ACCEPT (13), CLOSE (19) |
| `flags` | 1 | 1 байт | Флаги SQE (IO_LINK, IO_DRAIN и т.д.) |
| `ioprio` | 2 | 2 байта | Приоритет I/O |
| `fd` | 4 | 4 байта | File descriptor сокета |
| `off` | 8 | 8 байт | Смещение (для файлов) или адрес (для accept) |
| `addr` | 16 | 8 байт | Адрес буфера для данных |
| `len` | 24 | 4 байта | Длина буфера |
| `rw_flags` | 28 | 4 байта | Флаги операции (MSG_WAITALL и т.д.) |
| `user_data` | 32 | 8 байт | Произвольное значение, копируется ядром в CQE без изменений |
| `buf_index` | 40 | 2 байта | Индекс зарегистрированного буфера |

В коде заполнение SQE для recv:
```java
// IoUringNative.java — команда "прочитай данные из сокета fd в буфер buf"
public static void sqeSetRecv(MemorySegment sqe, int fd, MemorySegment buf,
                               int len, long userData) {
    sqe.set(JAVA_BYTE,  0,  (byte) IORING_OP_RECV);  // opcode = 27
    sqe.set(JAVA_INT,   4,  fd);                       // какой сокет
    sqe.set(JAVA_LONG,  16, buf.address());             // куда писать данные
    sqe.set(JAVA_INT,   24, len);                       // размер буфера
    sqe.set(JAVA_LONG,  32, userData);                  // метка для CQE
}
```

## 4. Структура CQE (результат от ядра) — 16 байт

| Поле | Смещение | Размер | Назначение |
|------|----------|--------|------------|
| `user_data` | 0 | 8 байт | Копия `user_data` из SQE — так приложение понимает, какой команде принадлежит результат |
| `res` | 8 | 4 байта | Результат: количество байт (при успехе) или `-errno` (при ошибке) |
| `flags` | 12 | 4 байта | Флаги CQE (IORING_CQE_F_MORE и т.д.) |

## 5. Кодирование user_data: как отличить recv от send

Когда из CQ приходит CQE, сервер должен понять: это результат recv, send или close? Для этого используется кодирование в поле `user_data`:

```
user_data (64 бита):
┌────────────────┬─────────────────────────────────────────┐
│  биты 63..56   │               биты 55..0                │
│  тип операции  │           file descriptor               │
│  RECV=2        │           (номер сокета)                 │
│  SEND=3        │                                         │
│  CLOSE=4       │                                         │
└────────────────┴─────────────────────────────────────────┘
```

В коде `WorkerThread.java`:
```java
static final long UD_RECV  = 2L << 56;   // 0x0200_0000_0000_0000
static final long UD_SEND  = 3L << 56;   // 0x0300_0000_0000_0000
static final long UD_CLOSE = 4L << 56;   // 0x0400_0000_0000_0000
static final long UD_FD_MASK = (1L << 56) - 1;

// При подаче SQE:
submitRecv(fd, ...);  // user_data = UD_RECV | fd = 0x0200...00 | 42

// При чтении CQE:
long opType = userData & ~UD_FD_MASK;  // выделить тип: RECV, SEND или CLOSE
int fd = (int)(userData & UD_FD_MASK); // выделить fd
```

Ядро просто копирует `user_data` из SQE в CQE без изменений. Это чисто прикладное соглашение.

## 6. Полный цикл обработки HTTP-запроса

Рассмотрим один HTTP-запрос `GET /?size=4096` от подключения к завершения:

```
 Приложение (Worker)                 SQ            Ядро Linux               CQ
 ─────────────────                 ──────          ──────────              ──────

 1. drainNewFds():
    fd=42 пришёл от acceptor
    submitRecv(42, buf[0])
         ──── SQE ──────►  [RECV fd=42       ──────►  ядро делает
                             buf=0x7f..00             recv(42, buf, 4096)
                             len=4096                 клиент прислал
                             ud=0x0200_0000_          "GET /?size=4096\r\n..."
                                0000_002A]

 2. ring.submit()            (публикует tail)

 3. ring.peekCompletions()                            ◄──── CQE ────
                                                     [ud=0x0200_0000_
                                                         0000_002A
                                                      res=89 (байт)]

 4. forEachCqe → handleRecv(42, 89):
    - читаем 89 байт из buf[0]
    - парсим: size=4096, keep-alive=false
    - берём pre-built response (HTTP 200 + 4096 байт тело)
    submitSend(42, response, 4234)
         ──── SQE ──────►  [SEND fd=42       ──────►  ядро делает
                             buf=0x7f..XX             send(42, response, 4234)
                             len=4234
                             ud=0x0300_0000_
                                0000_002A]

 5. ring.submit()            (публикует tail)

 6. ring.peekCompletions()                            ◄──── CQE ────
                                                     [ud=0x0300_0000_
                                                         0000_002A
                                                      res=4234 (байт)]

 7. forEachCqe → handleSend(42, 4234):
    - sendOffset (4234) == sendTotal (4234) → всё отправлено
    - keep-alive=false → submitClose(42)
         ──── SQE ──────►  [CLOSE fd=42      ──────►  ядро делает
                             ud=0x0400_0000_           close(42)
                                0000_002A]

 8. ring.submit()

 9.                                                   ◄──── CQE ────
                                                     [ud=0x0400_0000_
                                                         0000_002A
                                                      res=0 (ok)]

10. forEachCqe → handleClose(42):
    - освобождаем буфер и состояние соединения
```

## 7. Архитектура FFM-MT сервера: N+1 колец

В многопоточном FFM-MT сервере каждый поток имеет **собственный** io_uring instance (свою пару SQ+CQ):

```
┌──────────────────────────────────────────────────────────────────┐
│  Acceptor (main thread)                                          │
│  ┌──────────────────────────────────┐                            │
│  │  io_uring ring (256 entries)     │                            │
│  │  SQ: [ACCEPT] → переподача      │                            │
│  │  CQ: [fd=42] [fd=43] [fd=44]    │                            │
│  └──────────────┬───────────────────┘                            │
│                 │ round-robin через ConcurrentLinkedQueue         │
│       ┌─────────┼──────────┐                                     │
│       ▼         ▼          ▼                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                            │
│  │Worker 0 │ │Worker 1 │ │Worker 2 │  ...                        │
│  │ring 4096│ │ring 4096│ │ring 4096│                             │
│  │fixed buf│ │fixed buf│ │fixed buf│  (2048 x 4KB каждый)        │
│  │RECV/SEND│ │RECV/SEND│ │RECV/SEND│                             │
│  │/CLOSE   │ │/CLOSE   │ │/CLOSE   │                             │
│  └─────────┘ └─────────┘ └─────────┘                            │
└──────────────────────────────────────────────────────────────────┘
```

- **Acceptor ring** (256 entries): только операция `ACCEPT`. Получает fd новых клиентов из CQ, раздаёт worker'ам round-robin.
- **Worker ring** (4096 entries каждый): операции `RECV`, `SEND`, `CLOSE`. Весь I/O для назначенных соединений.
- **Между потоками нет разделяемых колец.** Каждый поток работает только со своим io_uring instance. Единственная точка синхронизации — `ConcurrentLinkedQueue` для передачи fd от acceptor к worker.

## 8. Режим SQPOLL: ядро само опрашивает SQ

В стандартном режиме после заполнения SQE приложение вызывает `io_uring_enter()` (syscall), чтобы ядро забрало команды из SQ. Это стоит ~100-200 нс на syscall overhead.

В режиме SQPOLL ядро создаёт отдельный kernel thread, который **непрерывно опрашивает** SQ ring:

```
Стандартный режим:                    SQPOLL режим:

App: пишет SQE в SQ                  App: пишет SQE в SQ
App: io_uring_enter() ← syscall      App: store fence + пишет tail
Kernel: читает SQ, выполняет         Kernel thread: видит новый tail
Kernel: пишет CQE в CQ               Kernel thread: читает SQ, выполняет
App: читает CQ                       Kernel thread: пишет CQE в CQ
                                      App: читает CQ
```

В FFM-MT сервере SQPOLL включён для worker'ов. Код в `IoUringRing.java`:

```java
// SQPOLL submit: вместо syscall — просто запись tail в shared memory
VarHandle.storeStoreFence();                       // барьер: все SQE видны
sqRing.set(JAVA_INT, sqTailOff, localTail);        // публикуем tail
VarHandle.storeStoreFence();                       // барьер: tail виден

// Если kernel thread уснул (idle > 1000ms), будим его
int sqFlags = sqRing.get(JAVA_INT, sqFlagsOff);
if ((sqFlags & IORING_SQ_NEED_WAKEUP) != 0) {
    ioUringEnterRaw(ringFd, 0, 0, IORING_ENTER_SQ_WAKEUP);  // один syscall
}
```

Выигрыш SQPOLL: экономия на syscall при каждом submit. Для высоконагруженного сервера с тысячами операций в секунду это может давать 5-15% прирост throughput.

## 9. Fixed buffers: зарегистрированная память

Обычно при `recv()` ядро должно для каждого вызова:
1. Найти виртуальный адрес буфера
2. Пройти page table (4 уровня на x86_64)
3. Закрепить страницы (`pin pages`)
4. Выполнить DMA/копирование
5. Открепить страницы

С **fixed buffers** шаги 1-3 и 5 выполняются **один раз** при регистрации, а не при каждом I/O-вызове. Для этого операция должна использовать опкод `IORING_OP_READ_FIXED` / `IORING_OP_WRITE_FIXED` с указанием `buf_index`.

```java
// Регистрация: один раз при старте worker'а
MemorySegment[] fixedBufferSegments = new MemorySegment[2048];
for (int i = 0; i < 2048; i++) {
    fixedBufferSegments[i] = arena.allocate(4096, 8);   // 4KB буфер
}
MemorySegment iovec = buildIovecArray(arena, fixedBufferSegments);
ring.registerBuffers(iovec, 2048);   // io_uring_register(REGISTER_BUFFERS)
```

**Нюанс текущей реализации FFM-MT:** буферы зарегистрированы через `REGISTER_BUFFERS`, но recv/send используют обычные опкоды `IORING_OP_RECV` / `IORING_OP_SEND` с передачей адреса буфера, а не `READ_FIXED`/`WRITE_FIXED` с `buf_index`. Ядро всё равно может оптимизировать доступ к зарегистрированным страницам (они уже pinned), но полное преимущество fixed buffers (нулевой page table lookup) реализуется только при использовании `*_FIXED` опкодов. Это потенциальная точка оптимизации.

В FFM-MT сервере: 2048 буферов по 4KB на worker, управляются через free-list.

## 10. Сравнение с epoll: почему io_uring принципиально отличается

| Аспект | epoll | io_uring |
|--------|-------|----------|
| **Модель** | Уведомление о готовности | Завершение операции |
| **Как работает recv** | `epoll_wait()` → «fd готов» → `read()` (2 syscall) | SQE[RECV] → CQE[результат] (1 syscall или 0 при SQPOLL) |
| **Batching** | `epoll_wait` возвращает batch готовых fd, но каждый `read()`/`write()` — отдельный syscall | SQ может содержать сотни команд, один `io_uring_enter()` отправляет все |
| **Kernel crossing** | 2N syscall на N операций (epoll_wait + N read/write) | 1-2 syscall на N операций (submit + wait) |
| **Shared memory** | Нет — данные копируются через syscall | SQ/CQ в mmap — нулевое копирование команд и результатов |
| **Kernel thread** | Нет | SQPOLL — ядерный поток опрашивает SQ без syscall |
| **strace видимость** | Видны все read()/write() | Видны io_uring_enter(), но не отдельные операции внутри |

## 11. Резюме

1. **io_uring — это не два канала «вход/выход».** Это пара «очередь команд (SQ) / очередь результатов (CQ)». Любая I/O-операция — recv, send, accept, close — проходит через одну и ту же пару.

2. **В памяти три mmap-региона:** SQ ring (индексы), SQE array (сами команды), CQ ring (результаты с CQE inline).

3. **Приложение различает результаты** по полю `user_data` (8 байт), которое ядро копирует из SQE в CQE без изменений. В FFM-MT сервере: старшие 8 бит = тип операции, младшие 56 бит = fd.

4. **FFM-MT сервер использует N+1 независимых колец:** 1 для acceptor (только ACCEPT), N для worker'ов (RECV/SEND/CLOSE). Кольца не разделяются между потоками.

5. **Оптимизации** (SQPOLL, fixed buffers) не меняют модель — они ускоряют взаимодействие с ядром, убирая syscall overhead и page table lookup.
