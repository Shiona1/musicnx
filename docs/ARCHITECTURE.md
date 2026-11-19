# Архитектура MusicNX

Документ описывает фактическую архитектуру P1. Источник требований — [SPEC.md](../SPEC.md)
§4; решения — [DECISIONS.md](DECISIONS.md). Всё, что помечено «позже», не реализовано
и не должно появляться в коде до своей фазы.

## 1. Взгляд сверху

```text
┌──────────────┐   HTTP REST + WebSocket   ┌────────────────────────────┐
│ Браузер      │ ────────────────────────► │ FastAPI (uvicorn, asyncio) │
│ (ПК/телефон) │ ◄──────────────────────── │  /api/v1, /api/v1/ws       │
└──────────────┘        WS-события         │  SPA static                │
                                           └────────────┬───────────────┘
                                                        │
                    ┌───────────────────────────────────┼─────────────────────────┐
                    │                                   │                         │
             ┌──────▼──────┐                    ┌───────▼────────┐        ┌───────▼───────┐
             │ Player /    │                    │  Library /     │        │  Adapters     │
             │ Queue /     │                    │  Catalog       │        │  Yandex (mock)│
             │ State       │                    │  (P1: mock)    │        │  Router (mock)│
             └──────┬──────┘                    └───────┬────────┘        │  BT (mock)    │
                    │                                   │                 │  Net (mock)   │
                    │                           ┌───────▼────────┐        └───────────────┘
                    │                           │ SQLite (WAL)   │
             ┌──────▼──────┐                    └────────────────┘
             │ AudioBackend│  mock: virtual clock
             │  ┌────────┐ │  real: mpv subprocess + JSON IPC (unix socket)
             │  │  mpv   │ │
             │  └────────┘ │
             └─────────────┘
```

Сервер — единственный источник истины. Браузер ничего не хранит как состояние:
при подключении получает снапшот, дальше применяет события. Любое действие —
REST-команда, сервер меняет состояние и рассылает событие всем клиентам.

## 2. Слои и границы

| Слой | Пакет | Ответственность | Не знает про |
|---|---|---|---|
| API | `app/api` | HTTP/WS-транспорт, валидация, коды ошибок | mpv, SQL |
| Core | `app/core` | очередь, state machine, восстановление, события | HTTP, mpv, Яндекс |
| Audio | `app/audio` | воспроизведение, IPC, зоны, BT | очередь, HTTP |
| Library | `app/library` | импорт/сканер, теги, обложки, FTS | HTTP, mpv |
| DB | `app/database` | подключение, миграции, репозитории | HTTP, аудио |
| Adapters | `app/integrations`, `app/network` | внешний IO за интерфейсом | очередь, SQL |
| Services | `app/services` | health/storage/служебные вычисления | HTTP |

Правила зависимостей:

- `api → core`, `core → audio.base (Protocol)`, `core → database.repositories`,
  `audio →` ничего из core.
- `PlayerManager` не знает, локальный это файл или URL: он получает `uri` от
  `PlayableSourceResolver`.
- `MpvAudioBackend` не знает про очередь: им управляет PlayerManager.
- Яндекс-адаптер не управляет mpv и не пишет в очередь.
- Внешний IO всегда за интерфейсом: `AudioBackend`, `MpvTransport`,
  `YandexAdapter`, `AudioRouter`, `BluetoothAdapter`, `NetworkAdapter`.

## 3. Жизненный цикл приложения

`create_app(settings)` собирает FastAPI и кладёт `AppContext` в `app.state.ctx`.
Lifespan:

1. Логирование (файл с ротацией; в тестах — только консоль).
2. Создание каталогов, подключение SQLite, `PRAGMA`, миграции.
3. Seed mock-каталога (только `mode=mock` и `mock.seed_catalog=true`).
4. Создание репозиториев, event bus, queue manager, audio backend, player.
5. `Game`: `backend.start()`, `player.restore()`, запуск тикера позиции.
6. `yield`.
7. Остановка: тикер отменяется, состояние и позиция сбрасываются в БД
   (`flush`), backend.shutdown(), БД закрывается.

`app/audio/factory.py` выбирает backend: `mock` → `MockAudioBackend`;
`mpv`/`auto` + Linux → `MpvAudioBackend`; `mpv` на non-POSIX → понятная
`ConfigurationError` (ADR-006).

## 4. Поток: воспроизведение из полки

```text
UI: POST /player/play {track_id}
  → PlayerManager.play(track_id)
      → QueueManager.ensure_track(track_id)   # добавляет в конец, если нет
      → TrackRepository.get(track_id)
      → PlayableSourceResolver.resolve(track) # mock://… или /path/file
      → state buffering; publish player.state_changed
      → AudioBackend.load(uri, start_ms)
      → AudioBackend.play(); state playing
      → publish player.track_changed + player.state_changed
      → PlaybackStateRepository.save(...)     # сразу
```

Тикер (250 мс) при `playing`:

- читает позицию у backend (mock — вычисляет по monotonic clock; mpv — кэш
  observe `time-pos`);
- не чаще 2 Гц публикует `player.position` (WS-роут отфильтровывает скрытые вкладки);
- раз в `position_persist_interval_s` (7 с) пишет позицию в SQLite;
- при EOF решает: следующий трек (если `autoplay`) или `stopped`.

## 4.1 Поток: импорт медиатеки (P2)

```text
UI: POST /library/import (PIN/localhost)
  → ImportManager.start()  (один job, 202)
  → task:
      discover media files (os.walk в thread, только import_roots)
      mark missing known files
      для каждого файла:
        mutagen probe (thread) → LocalMediaMetadata + embedded cover
        LibraryRepository.import_local (транзакция):
          source_id+external_id → dedupe metadata → insert track/media_file
          index_track в FTS5
        cover → Pillow 300/1000/64 + LRU → albums.cover_path
      verify: SHA-256 в thread → media_files.verified_at + has_local_file=1
      WS progress/completed + library.updated
```

Тяжёлый IO не в event loop: walk/mutagen/hash/Pillow — `asyncio.to_thread`.
Ошибка файла не прерывает job. Отмена проверяется между файлами. Импорт не
ходит в сеть и не меняет локальные данные при недоступности сети.

## 4.2 Поток: Яндекс Музыка (P3)

```text
UI Setup → POST /yandex/auth/device/start (PIN/localhost)
  → YandexAuthManager.request_device_code (executor/async)
  → QR/user_code; фоновый poll_device_token
  → token store 0600 → WS auth.status
Каталог: /yandex/likes|playlists|album|artist|search
  → YandexClient (только YANDEX_API.md) → mapper → YandexRepository upsert
  → внутренние Track/Album/Artist → UI
Playback: POST /player/play → CompositeSourceResolver
  → local media? файл : YandexStreamingService (direct link)
  → mpv сам открывает URL; Python не тащит байты
Браузер: GET /stream/{track_id} → FileResponse (local) или aiohttp Range proxy
Browser zone: audio.browser_zone → mpv mute; клиент <audio> + WS position sync
```

Токены не выходят за `YandexAuthManager`; mock-адаптер реализует тот же
`YandexAdapter` и не ходит в сеть.

## 5. Поток: WebSocket

```text
connect
  → проверка Origin/Host (те же правила, что для HTTP)
  → accept; subscribe() в EventBus (bounded queue 256, drop-oldest)
  → snapshot: server, player, queue, shelf
  ⇄ client.hello {visible} → подписка/отписка на player.position
  ⇄ ping → pong
  ← события {type, ts, payload}
```

Каждому клиенту — своя очередь. Медленный клиент теряет старейшие события, но
не блокирует сервер; полное состояние всегда доступно снапшотом или REST.
Скрытая вкладка не получает позицию, но получает `state_changed`, `track_changed`,
`queue.updated` — при возврате UI сразу корректен (плюс локальная интерполяция).

## 6. Поток: сохранение и восстановление

- Очередь хранится в `queue_items` (позиции 0..n-1, уникальны).
- Текущий элемент — `playback_state.queue_item_id`, позиция — `position_ms`,
  статус, громкость, зона.
- Позиция пишется: по debounce 7 с, при pause/stop/seek/eof/смене трека/остановке
  приложения. Не каждый тик (SPEC §11.2).
- При старте: очередь → состояние; `playing/buffering` превращается в `paused`
  на сохранённой позиции (ADR-004), backend ничего не загружает до Play.

## 7. Ошибки и деградация

- Нет доступного источника трека (real-режим, нет `media_files`) →
  `409 no_playable_source`, статус `error`, событие `system.warning`.
- mpv упал → supervisor перезапускает с backoff, перезагружает текущий URI и
  позицию; при неудаче статус `error`, клиент видит предупреждение.
- Ошибки БД при старте → приложение не поднимается (лучше явно, чем тихо без
  состояния).
- Любая ошибка REST — конверт `{"error": {code, message, details}}` (ADR-008).
- Неизвестный трек/элемент очереди → 404; неверный диапазон → 422.

## 8. Конкурентность

- Один SQLite-коннект (`aiosqlite`), запись — через `asyncio.Lock` и транзакции.
- Все операции PlayerManager сериализованы `asyncio.Lock`; тикер берёт тот же lock
  короткими секциями.
- `subprocess` mpv — только `asyncio.create_subprocess_exec`, аргументы списком.
- Блокирующих вызовов в event loop нет; диск/`shutil.disk_usage` — через
  `asyncio.to_thread`, где нужно.
- Фоновых «тяжёлых» задач на P1 нет, кроме тикера и mpv-супервизора.

## 9. Конфигурация и режимы

`Settings` (pydantic-settings): YAML → env `MUSICNX_*` (`__` для вложенности) →
`.env` → дефолты. `mode=mock|real` определяет backend и источники.

- **mock** — всё виртуально: `MockAudioBackend`, mock-адаптеры, seed-каталог.
  Полный e2e без сети, токена и Pi.
- **real** — mpv; локальные файлы из `media_files`; Яндекс/зоны/BT ещё не
  реализованы и репортят `available: false` в `/system/status`.

## 10. Структура кода (P1)

```text
backend/app/
├── main.py              app factory, lifespan, SPA, middleware, error handlers
├── config.py            Settings + YAML/env sources
├── logging_setup.py     dictConfig, rotation, redaction
├── context.py           AppContext (singletons)
├── errors.py            AppError -> REST envelope
├── api/routes/          system.py player.py queue.py library.py
├── api/websocket.py     /api/v1/ws
├── core/                player_manager.py queue_manager.py state_machine.py
│                        availability.py event_bus.py models.py
├── audio/               base.py mock_backend.py mpv_ipc.py mpv_backend.py
│                        factory.py router.py bluetooth.py
├── library/             probe.py importer.py covers.py
├── database/            connection.py repositories.py seed.py migrations/
├── integrations/yandex_music/  base.py auth.py token_store.py client.py
│                        catalog.py repository.py streaming.py wave.py
│                        mock_adapter.py service.py
├── api/routes/          yandex.py yandex_auth.py stream.py
├── network/manager.py   adapter + mock
├── downloads/paths.py   путь загрузок (чистая функция, тесты P1)
├── services/storage.py  disk usage
└── devtools/register_track.py  dev-фикстура для real-режима (не импорт P2)
```

## 11. Что появится позже и где

| Фаза | Точка расширения |
|---|---|
| P2 | `library/` importer/probe/covers, FTS-заполнение | реализовано |
| P3 | `integrations/yandex_music/`, `/yandex`, `/stream`, mock adapter | реализовано (mock) |
| P4 | downloads/encraw, offline shelf |
| P4 | `downloads/` pipeline + очередь |
| P5 | `lyrics/` LRC, offset |
| P6 | `sync/` scheduler/diff |
| P7 | `audio/zones.py` + PipeWire/BlueZ |
| P8 | PIN-полишинг, backup API, Wi-Fi/AP |

Границы уже существуют (интерфейсы, таблицы, конфиг), реализации нет — и это
сознательно, чтобы не выпускать недоделанные функции под видом готовых.
