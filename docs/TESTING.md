# Тестирование

Требования — [SPEC.md](../SPEC.md) §13 (тесты по фазам). Принцип: тесты не ходят
в сеть, не требуют токена, mpv и Raspberry Pi. Всё внешнее — за mock-интерфейсами.

## 1. Команды

```bash
make test                    # pytest, mock-режим, tmp БД
make lint                    # ruff + svelte-check
make check                   # lint + test
uv run --directory backend pytest -q -k websocket
uv run --directory backend pytest --cov=app --cov-report=term-missing   # если установлен pytest-cov
```

Python: 3.11+ (dev — 3.13), pytest + pytest-asyncio (`asyncio_mode=auto`),
tmp_path для БД/каталогов. Каждый тест создаёт `Settings` с
`logging.file=None` и временными путями — файловая система и БД изолированы.

## 2. Пирамида

| Уровень | Что | Где |
|---|---|---|
| Unit | чистые функции: доступность, шаблон путей, state machine, IPC-кадры | `test_availability.py`, `test_paths.py`, `test_state_machine.py`, `test_mpv_ipc.py` |
| Component | очередь в БД, event bus, миграции, config | `test_queue_manager.py`, `test_event_bus.py`, `test_migrations.py`, `test_config.py` |
| API | REST + WS через `TestClient`, реальный lifespan | `test_api_*.py`, `test_websocket.py` |
| E2E (mock) | play → позиция → pause/seek/next → restore | `test_mock_e2e.py`, `test_player_restore.py` |

Внешних сервисов нет: SQLite — временный файл (или `:memory:` не используется,
WAL требует файл), mock backend — виртуальные часы от `time.monotonic()`.

## 3. Матрица P1 (SPEC §13)

| Требование | Тест |
|---|---|
| Очередь: add/remove/reorder, уникальность позиций, повторы трека | `test_queue_manager.py`, `test_api_queue.py` |
| State machine: допустимые/запрещённые переходы | `test_state_machine.py` |
| Шаблон путей: санитизация, год, unicode, traversal | `test_paths.py` |
| Вычисление доступности (5 бейджей) | `test_availability.py` |
| Восстановление после рестарта (paused) | `test_player_restore.py` |
| WS-снапшот, события, позиция 2 Гц, скрытые вкладки | `test_websocket.py` |
| Синхронизация двух вкладок | `test_websocket.py::test_two_clients_sync` |
| REST health/status/storage | `test_api_system.py` |
| Player play/pause/seek/next/volume | `test_api_player.py` |
| Mock e2e без токена/Pi | `test_mock_e2e.py` |
| Seed идемпотентен | `test_queue_manager.py::test_seed_is_idempotent` |
| Host/Origin защита | `test_security.py` |
| Секреты не попадают в логи | `test_logging.py` |
| mpv IPC без mpv (fake transport) | `test_mpv_ipc.py` |
| Конфиг: YAML/env приоритет | `test_config.py` |

LRC parser — P5, тестов в P1 нет (SPEC §13 допускает перенос).

## 3.1 Матрица P2 (SPEC §13 P2)

| Требование | Тест |
|---|---|
| Импорт MP3/FLAC/M4A/WAV, теги, длительность | `test_library_import.py`, фикстуры `tests/fixtures/media` |
| Дедупликация: external ID → metadata; не склеивать по album/duration/artist | `test_library_dedupe.py` |
| Идемпотентность повторного импорта | `test_library_import.py::test_import_is_idempotent` |
| Traversal/недопустимый путь | `test_library_import.py::test_import_rejects_path_outside_roots` |
| Checksum → verified/offline; пропавший файл → missing без удаления | `test_library_import.py::test_missing_file_marks_track_without_deleting_it` |
| FTS5 поиск, Unicode, альбомы/исполнители, пагинация | `test_library_search.py` |
| Обложки 300/1000/64, LRU, temp cleanup | `test_covers.py`, artwork-проверка в import-тесте |
| WS-прогресс импорта и `library.updated` | `test_library_ws.py` |
| Import без PIN/с localhost/Origin/cookie | `test_auth_mutations.py`, `test_auth.py` |
| Критичные события не теряются при drop-oldest | `test_event_bus.py` |
| Ошибка одного файла не валит весь job | `test_library_import.py::test_import_continues_after_corrupt_file` |

Импорт выполняется в `asyncio.to_thread` (mutagen, SHA-256, Pillow), БД — async;
тесты не требуют сети, токена, mpv и Pi. Аудиофикстуры — реальные MP3/FLAC/M4A
с тегами и embedded JPEG, WAV генерируется стандартной библиотекой.

## 3.2 Матрица P3 (Yandex, SPEC §13 P3)

| Требование | Тест |
|---|---|
| Замороженные методы, интроспекция 3.2.2 | `docs/YANDEX_API.md` (ручной gate); `test_yandex_fixtures.py` проверяет DTO-формы |
| Device Flow: pending→authorized, QR, manual token, logout | `test_yandex_auth.py`, `integration/test_p3_e2e.py` |
| Токены не в API/WS/логах/фикстурах | `test_yandex_fixtures.py`, `test_yandex_auth.py` |
| Refresh proactive/401 retry/failure → auth.required | `test_yandex_auth.py` |
| Likes/playlists/album/artist/search, пагинация | `test_yandex_catalog.py`, e2e |
| Нормализация в общие tracks + track_sources | `test_yandex_catalog.py` |
| `/stream` Range 206/Content-Range, без RAM-буфера | `test_yandex_stream.py`, e2e |
| Preview 30 сек, region/auth ошибки | fixtures `error_*.json`, mock e2e |
| Wave start/feedback/skip/refill | `test_yandex_wave.py`, e2e |
| Две вкладки, WS snapshot/events | `integration/test_p3_e2e.py` |
| Browser zone (mpv muted, `<audio>`) | UI-компонент проверен статически; real Pi — pending |

Mock-прогон: `uv run --directory backend pytest -q`; реальная сеть/токен/Pi в
CI не используются. Real-smoke — отдельный manual-тест (Device Flow владельца),
не в CI; на Pi только при отсутствии активных throttling-битов.

## 3.3 Матрица P4 (Downloads)

| Требование | Тест |
|---|---|
| Очередь 1, priority, anti-ban, restart | `test_download_manager.py`, e2e |
| retry/backoff, cancel/delete, dedup | `test_download_manager.py`, e2e |
| quality fallback FLAC→AAC→MP3 | `test_download_pipeline.py`, e2e |
| direct/encraw AES-CTR, Range resume | `test_download_pipeline.py` |
| atomic write/layout/traversal/tags/cover | `test_download_pipeline.py`, e2e |
| verify→offline, `/stream` local | e2e, real smoke |
| UI Downloads/offline/PIN | svelte-check + mock browser |

Real download одного трека выполнен на Pi; полный album/playlist + soak —
pending. Anti-ban пауза 2–5 с тестируется с укороченными настройками.

## 3.4 Матрица P5 (Lyrics)

| Требование | Тест |
|---|---|
| LRC теги/несколько меток/offset/Unicode/plain | `test_lyrics_parser.py` |
| Кэш/приоритет LRC/offset/delete | `test_lyrics_repository.py` |
| local .lrc/Yandex fake/cache/timeout | `test_lyrics_service.py` |
| GET open, PUT PIN/422/409 | `test_lyrics_api.py` |
| UI подсветка/seek/offset | svelte-check + mock browser |

## 3.5 Матрица P6 (Sync)

| Требование | Тест |
|---|---|
| repository/runs/history | `test_sync.py`, fixtures |
| single-flight/cancel/partial/backoff | `test_sync.py`, e2e |
| manual API + PIN | `test_sync.py` |
| Sync Center/WS | svelte-check + mock e2e |
| hourly only when enabled | service test |

Real hourly sync на Pi — pending soak.

## 3.6 Матрица P7 (Zones/BT)

| Требование | Тест |
|---|---|
| zone repo/defaults/activation/volume/offset | `test_zones.py` |
| sink switch без stop player | `test_zones.py` |
| mock/real router fallback, no shell injection | `test_zones.py` |
| BT devices/scan/pair/connect/forget | `test_bluetooth.py` |
| PIN/404/503 routes | `test_zones.py`, `test_bluetooth.py` |
| ZonesPanel | svelte-check + mock browser |

Real jack/USB-DAC/BT на Pi — pending hardware; `bluetoothctl`/`pactl` fallback
проверяется на месте.

## 3.7 Матрица P8 (Reliability)

| Требование | Тест |
|---|---|
| Backup API/list/create/verify/traversal | `test_backups.py` |
| integrity/maintenance cleanup | `test_maintenance.py` |
| settings whitelist/PIN/persist/apply | `test_settings.py` |
| scripts синтаксис/поведение | `bash -n` + WSL harness (P8_NOTES) |
| Settings UI | svelte-check + mock browser |

24h soak, Wi-Fi/AP, PIN rotation/TLS — pending; real Pi backup/settings — smoke.

## 4. Mock e2e: как это работает

`create_app(Settings(mode="mock", ...))` в `TestClient` запускает lifespan:
миграции, seed, `MockAudioBackend`, `PlayerManager` с тикером 250 мс.
`MockAudioBackend` считает позицию как `base + (monotonic - started)`, поэтому:

- play → сразу `playing`, позиция растёт без реального аудио;
- pause → позиция фиксируется;
- seek → меняет базу;
- тест ждёт `time.sleep(0.6)` (блокирующий sleep в тесте, не в event loop),
  затем `GET /player/state` и проверяет рост позиции;
- EOF проверяется коротким треком (в seed есть трек 2 с) + ожиданием.

Тикер публикует `player.position` не чаще 2 Гц; тест WS читает 1–2 события и
проверяет `ts`/`position_ms`, не полагаясь на точное число.

## 5. Real-режим (Linux/Pi)

Проверено на Raspberry Pi 3 B (32-bit Raspberry Pi OS 13, mpv 0.40, PipeWire
1.4.2) 2026-11-14 — результаты и ограничения в [HARDWARE.md](HARDWARE.md) §9.
Автотест с mpv помечен `skipif` (нет mpv / не POSIX) и не входит в `make test`
на Windows. Ручная проверка:

```bash
# 1. real-конфиг: mode: real, audio.backend: mpv; backend сам запускает mpv.
#    Не запускать второй mpv вручную — он будет конкурировать за IPC-сокет.
#    Для одного файла можно использовать dev-фикстуру:
cd backend
uv run python -m app.devtools.register_track /path/to/track.flac
# 2. импорт (P2, после замены БП): UI «Медиатека» или POST /library/import
# 3. запустить real
MUSICNX_CONFIG="$PWD/../config/config.yaml" MUSICNX_MODE=real uv run uvicorn app.main:app --port 8080
# 4. в UI: трек на полке → Play → слышен звук, позиция идёт, seek работает
```

Ожидаемо: `system/status` → `audio.backend: "mpv"`, `ready: true`; mpv-процесс
виден в `ps`; при `kill <mpv pid>` supervisor перезапускает его (в логе
`mpv_restarted`), воспроизведение продолжается с последней позиции.

## 5.1 Ручная проверка P2 (mock/dev)

```bash
make mock
# положить три файла в каталог данных paths.library — при `make mock` это
# backend/var/library (uv запускает приложение из backend/):
#   one.mp3, one.flac (одинаковые теги/альбом/длительность) → один трек, 2 media_files
#   two.flac (другое название/альбом) → отдельный трек
```

1. Открыть «Медиатека» → «Импортировать» (на localhost PIN не нужен).
2. Дождаться «Готово»: `новых`, `обновлено`, `пропущено`, `файлов`; ошибки видны
   в панели. Обложки появляются в списке/Now Playing.
3. Поиск: ввести название/исполнителя; FTS5 должен найти; фильтр «Офлайн»
   показывает проверенные локальные файлы, «Файл пропал» — после удаления файла
   и повторного импорта.
4. Дедупликация: `GET /library/tracks` содержит один Shared Song на Test Album,
   но в БД два `media_files` (MP3+FLAC). Повторный импорт — `skipped=все`,
   количество треков не меняется.
5. API/без UI:
   ```bash
   curl -s -X POST localhost:8080/api/v1/library/import
   curl -s localhost:8080/api/v1/library/import/status
   curl -s "localhost:8080/api/v1/library/tracks?search=Shared"
   curl -s "localhost:8080/api/v1/library/search?q=Unicode"
   ```
6. PIN: если задан `security.pin`, удалённый `POST /library/import` без cookie —
   `401 auth_required`; без PIN — `403 pin_required`. После `POST /auth/pin`
   cookie `HttpOnly/SameSite=Strict`; импорт проходит.

## 5.2 Real Yandex smoke (dev, manual; не CI)

Нужен аккаунт с подпиской и токен владельца. Сеть/Pi в автотестах не
используются.

```bash
# 1. real-режим, mock-аудио не нужен; запускать с dev-машины
export MUSICNX_MODE=real
export MUSICNX_YANDEX__ENABLED=true
export MUSICNX_YANDEX__TOKEN_FILE="$PWD/var/secrets/yandex_token.json"
export MUSICNX_CONFIG="$PWD/config/config.yaml"
uv run --directory backend uvicorn app.main:app --port 8080

# 2. Device Flow (localhost; PIN не нужен)
curl -s -X POST localhost:8080/api/v1/yandex/auth/device/start | python -m json.tool
# открыть verification_url, ввести user_code, затем:
curl -s "localhost:8080/api/v1/yandex/auth/device/status?flow_id=<flow_id>"
curl -s localhost:8080/api/v1/yandex/auth/status   # authenticated=true, без токенов

# 3. каталог/волна
curl -s "localhost:8080/api/v1/yandex/likes?limit=5"
curl -s -X POST localhost:8080/api/v1/yandex/wave/start
curl -s -X POST localhost:8080/api/v1/yandex/wave/feedback \
  -H 'Content-Type: application/json' \
  -d '{"action":"skip","track_id":<id>,"played_seconds":10}'

# 4. streaming Range (взять id из likes; ожидать 206 + Content-Range)
curl -s -D - -o /tmp/chunk.bin -H 'Range: bytes=0-1023' \
  localhost:8080/api/v1/stream/<track_id>
# 5. в браузере: Setup → likes → play/seek/next → wave → перемотка
```

Ожидаемо: `/stream` 206 и `Accept-Ranges: bytes`; токен не появляется в
REST/WS/HTML/логах; при истечении direct link сервер обновляет ссылку (403/410
→ повтор). При ошибке региона — `no_playable_source`; при отзыве токена —
`auth.required`.

## 5.3 Pi streaming-smoke (только без активных throttling bits)

`vcgencmd get_throttled` должен иметь биты 0/2 = 0. Разрешён только короткий
`/stream` Range/воспроизведение; import/checksum/soak — после БП. Real Yandex
на Pi — только после токена владельца и при отсутствии under-voltage.

## 6. Ручная проверка синхронизации двух вкладок

1. `make mock`, открыть `http://127.0.0.1:8080` в двух вкладках.
2. Вкладка A: нажать Play на треке. Вкладка B (без перезагрузки) показывает
   тот же трек и статус, прогресс движется.
3. B: Pause → A сразу показывает паузу; B: seek на 1:00 → A перематывается.
4. A: Next → обе показывают следующий трек; очередь обновилась.
5. Свернуть одну вкладку (скрыть) — она перестаёт получать позицию (проверка по
   DevTools → WS), при возврате снапшот/состояние корректны.
6. Перезагрузить сервер (`Ctrl+C`, `make mock`) → обе вкладки переподключились
   (backoff), очередь и позиция на месте, статус `paused`.

Автоматизированный аналог — `test_two_clients_sync` и Playwright (опционально,
dev-машина; браузер на Pi не запускается).

## 7. Стиль тестов

- Имена: `test_<behavior>_<condition>`.
- Arrange/Act/Assert без комментариев-очевидностей.
- Не мокать то, что можно проверить реально (SQLite, FastAPI); мокать только
  внешний IO (mpv, Яндекс, сеть).
- Никаких `sleep` больше 1 с; для позиции — 0.6 с.
- Тесты не зависят от порядка и от локального времени (UTC).
- Фикстуры: `settings`, `app`, `client`, `db`, `bus` — в `conftest.py`.

## 8. CI (план)

```yaml
# GitHub Actions (dev-машина, без Pi)
- setup python 3.12, uv
- uv sync --directory backend --frozen
- uv run --directory backend ruff check .
- uv run --directory backend pytest -q
- setup node 22, pnpm
- pnpm --dir frontend install --frozen-lockfile
- pnpm --dir frontend run check
- pnpm --dir frontend build
```

Сеть в тестах запрещена; если тест пытается — это баг теста. Секреты в CI не
нужны. mpv-интеграция — отдельный job на Linux с `apt install mpv` (P3+).
P2-импорт тестируется на mock-аудиофикстурах; real-import на Pi — только после
замены БП (docs/HARDWARE.md §9).

## 9. Покрытие и качество

Цель P1/P2 — не процент, а покрытие критичных путей: state machine, очередь,
восстановление, WS-контракт, доступность, пути, импорт/дедупликация/обложки,
FTS, checksum, security. Ruff — обязательный, без игнорирования ошибок в новом
коде. Падение теста = блокер коммита (AGENTS.md §5).

Текущий прогон (2026-11-14): **644 passed**, `ruff` и `svelte-check` чистые;
P3 e2e 5, P4 e2e 11, P5 52, P6 sync, P7 zones/BT mock.

