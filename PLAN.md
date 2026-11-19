# MusicNX — план разработки

Источник истины — [SPEC.md](SPEC.md). Этот план ведётся по фазам SPEC §13.
Одна фаза за раз: не начинать следующую, пока текущая не закрыта тестами и отчётом.

**Текущая фаза:** P0-стабилизация по `docs/EXTERNAL_REVIEW_ACTIONS.md`
(питание, security decision, soak, docs, deploy, flaky). P1–P3 — по плану ревью.
**Обновлено:** 2026-11-16.

## Статусы

- `[ ]` — не начато
- `[~]` — в работе
- `[x]` — завершено (тесты зелёные, отчёт в конце фазы)

---

## P0 — Bootstrap

Цель: репозиторий, документы, рабочий каркас backend/frontend, mock-режим без сети и без Pi.

- [x] Репозиторий, `.gitignore`, `SPEC.md` verbatim
- [x] `AGENTS.md` с правилами для агентов
- [x] `README.md`, `PLAN.md`, `docs/DECISIONS.md`
- [x] Полный набор docs SPEC §17 (ARCHITECTURE, DATABASE, API, AUDIO, SYNC,
      DOWNLOADS, UI, SECURITY, DEPLOYMENT, HARDWARE, ROADMAP, LEGAL, TESTING)
- [x] `config/config.example.yaml`, `.env.example`
- [x] Backend: FastAPI app factory, конфиг, логирование с ротацией, event bus
- [x] SQLite: подключение (WAL, `foreign_keys=ON`, `synchronous=NORMAL`), миграции,
      `schema_migrations`, репозитории
- [x] Mock-адаптеры: Yandex Music, Audio Router, Bluetooth, Network
- [x] Реально работающие маршруты P0: `GET /system/health`, `GET /system/status`,
      `GET /system/storage`, `GET /library/tracks` (mock-каталог)
- [x] Frontend: Svelte + Vite + TS, WS-клиент, минимальная страница, сборка в static
- [x] Тесты: инфраструктура, event bus, миграции, health, шаблон путей,
      вычисление доступности (LRC — P5)
- [x] `pyproject.toml` с пинами, ruff, pytest
- [x] Dev-команды `Makefile` (dev, mock, test, lint, build)
- [x] `ruff check` и `pytest` зелёные

**Выход P0:** `make mock` поднимает backend, отдаёт health/status и mock-полку;
`tests` проходят без сети, токена и Pi.

---

## P1 — Core + минимальный Web UI

Цель: mpv IPC, очередь, state machine, сохранение/восстановление, WS-шина, REST,
минимальный UI, mock e2e и real на локальном треке.

- [x] `MpvController`: subprocess + JSON IPC (unix socket), observe `time-pos`,
      `duration`, `idle-active`, `eof-reached`, команды, автоперезапуск
- [x] Транспорт IPC за интерфейсом, юнит-тесты кадрирования/корреляции без mpv
- [x] Mock audio backend с реальным течением времени (для e2e без mpv)
- [x] `QueueManager`: add/remove/reorder, персистентность в `queue_items`
- [x] State machine `stopped | playing | paused | buffering | error`, guard-переходы
- [x] Восстановление очереди/трека/позиции после рестарта
- [x] Debounce-запись позиции (7 с + pause/stop/seek/track change/shutdown)
- [x] Event bus → WS: снапшот при подключении, события, позиция 2 Гц
- [x] Позиция только «активным» (видимым) клиентам
- [x] REST: `player`, `queue`, `system`
- [x] Минимальный UI: полка, play/pause/seek/next/prev, очередь, статус, громкость
- [x] Синхронизация двух вкладок через WS (без перезагрузки)
- [x] Mock e2e без токена и без Pi
- [x] Real-режим: mpv + локальный тестовый трек (проверено на Pi 3, docs/HARDWARE.md §9)
- [x] Тесты: очередь, state machine, шаблон путей, доступность, WS, restore

**Выход P1:** две вкладки показывают одно состояние; mock-трек играет, пауза/seek/next
работают; после рестарта очередь и позиция на месте.

---

## P1.2 — Закрепление и мини-безопасность

Цель: постоянный запуск на Pi, защита будущих mutations, санкционированные
поправки SPEC.

- [x] Постоянный `~/.config/systemd/user/musicnx.service` под параметризованным
      пользователем; `WorkingDirectory`, venv uvicorn, `MUSICNX_CONFIG`,
      `Restart=always`, `RestartSec=2`, mpv socket через `%t`,
      `ExecStartPre mkdir %t/musicnx`
- [x] `systemctl --user enable --now musicnx`; linger включён
- [x] Reboot-проверка: UI/health поднимаются, очередь/трек восстановлены `paused`
- [x] `gpu_mem=16` в `/boot/firmware/config.txt`; применено после reboot
- [x] PIN **удалён владельцем** (15.11): `require_mutation_access` — no-op,
      LAN-мутации открыты; это residual risk, усиление — ADR-EX-001
- [x] Тесты auth/Origin/cookie/localhost; PIN не попадает в статус/логи
- [x] SPEC §2: 32-bit armv7l допускается (ADR-019); SPEC §12: hostname/user
      параметризованы (ADR-020)
- [x] Gate P4: `yandex-music[async,crypto]` ставится на armv7l (docs/HARDWARE.md §9)
- [x] requirements.txt синхронизирован; event bus сохраняет критичные события
      при переполнении; open question «доставка frontend dist» записан в DECISIONS
- [x] docs/DEPLOYMENT.md, docs/AUDIO.md, docs/SECURITY.md, docs/HARDWARE.md обновлены

**Выход P1.2:** Pi переживает reboot с MusicNX и восстановленным состоянием;
модель LAN-доступа зафиксирована (PIN удалён, residual risk); лимиты честно описаны.

---

## P2 — Media Library

Статус: **завершено в mock** (2026-11-14); real-import на Pi — pending до замены БП.

- [x] Сканирование по запросу/событию, импорт MP3/FLAC/AAC/M4A/WAV
- [x] Mutagen-теги (title/artists/albumartist/album/year/genre/track/disc/label/
      duration/bitrate/sample_rate/channels), обложки (300/1000/64px), LRU-кэш
- [x] Дедупликация: source_id+external_id → title+artists+album+duration
      (допуск 1500 мс), без склейки только по названию
- [x] Поиск FTS5, фильтры доступности, пагинация, альбомы/исполнители
- [x] Checksum в фоне, `media_files`, `verified_at`, offline только после проверки
- [x] Пропавший файл → `file_missing`, трек/альбом не удаляются
- [x] API library: tracks/albums/artists/search/upload/import/status/cancel/artwork
- [x] UI Library: вкладки, фильтры, импорт+прогресс, загрузка файлов из браузера, обложки
- [x] Импорт: PIN удалён, LAN открыт; тесты traversal/dedupe/checksum/FTS/LRU/WS
- [ ] **Real-import на Pi после замены БП** (pending; не запускать при throttling)

**Выход P2:** локальные файлы импортируются идемпотентно, находятся поиском,
имеют обложки и честный offline-статус; повторный импорт не создаёт дублей.

## P3 — Yandex: auth + каталог + стриминг + Волна

Статус: **real-стриминг восстановлен 15.11.2026**: CDN-edge `-03` был
недоступен, `CdnRouter` переключает подписанную ссылку на соседний edge `-02`
(см. [docs/YANDEX_INVESTIGATION.md](docs/YANDEX_INVESTIGATION.md)); mpv и
`/stream` проверены. Методы заморожены в [docs/YANDEX_API.md](docs/YANDEX_API.md),
`yandex-music==3.2.2`.

- [x] OAuth Device Flow, QR, ручной токен, refresh, файл 0600
- [x] Каталог: плейлисты, лайки, альбомы, исполнители, поиск, нормализация
- [x] Стриминг: download_info, выбор качества, обновление ссылок
- [x] Browser zone: `/stream/{track_id}` + Range, ресинк по WS
- [x] «Моя волна» + feedback
- [x] UI Setup, Home, Now Playing (онлайн)
- [x] UI каталога: плейлисты, альбомы, исполнители, поиск; скачивание коллекций
- [x] Mock-first e2e: setup→likes→play→seek→next, wave, Range, две вкладки
- [x] Real Yandex smoke: аккаунт/каталог/плейлисты, `/stream` 206, mpv; один CDN-edge недоступен (сеть)
- [ ] Проверить остальные CDN-edge маршруты; import/checksum/soak на Pi — после БП

**Выход P3:** mock-аккаунт подключается через Setup, лайки/плейлисты играют,
волна работает, `/stream` отдаёт Range; real-адаптер собран на подтверждённых
методах и ждёт токен владельца.

## P4 — Downloads + офлайн

Статус: **real download работает** (15.11.2026: трек 5 → `m4a`, offline);
полный альбом/плейлист и soak — после стабилизации CDN/питания.

- [x] Gate перед стартом: `yandex-music[async,crypto]` ставится на armv7l (docs/HARDWARE.md §9)
- [x] Очередь 1 параллельно, пауза 2–5 с, ретраи/backoff, докачка, restart-safe
- [x] Качество FLAC → AAC → MP3 320 + честный бейдж фактического формата
- [x] encraw (AES-128-CTR, stream) и direct; без транскодирования
- [x] Раскладка `Artist/Album (Year)/Disc X/NN - Title.ext`, traversal guard
- [x] Теги mutagen, cover.jpg 1000px, `.lrc` hook (P5)
- [x] SHA-256 verify → offline только после verified_at; dedup skip
- [x] UI Downloads/офлайн-полка, cancel/retry/delete
- [x] Выбор качества загрузок (`downloads.quality`: auto/flac/aac/mp3)
- [x] Real download одного трека на Pi (flac-mp4 → m4a, offline, `/stream` 206)
- [ ] Полный real-download альбома/плейлиста и soak — после стабилизации сети/БП

## P5 — Lyrics / караоке

Статус: **real Yandex lyrics + LRCLIB fallback** (15.11.2026); караоке/offset
проверены, LRCLIB включается `lyrics.lrclib_enabled`.

- [x] Свой LRC parser + тесты, plain fallback
- [x] Кэш SQLite + `.lrc` sidecar, offset
- [x] LRCLIB fallback, если нет локального LRC и Яндекс-текста
- [x] Подсветка текущей строки, клик/Enter seek
- [x] UI караоке-панель (LyricsPanel); offset — mutation (PIN removed)
- [x] WS `lyrics.updated`, mock lyrics
- [x] Real Yandex lyrics на треке с текстом (track 8, 24 LRC lines)

## P6 — Sync Engine

Статус: **завершено в mock** (2026-11-14); real hourly sync на Pi — pending
отдельного soak.

- [x] Hourly 3600 с / manual / on startup (при enabled)
- [x] Diff composition + fallback; идемпотентность
- [x] `sync_runs`, история, partial success, 429/5xx backoff, single-flight
- [x] WS sync.started/progress/completed/failed/cancelled
- [x] UI Sync Center, история, запуск/отмена

## P7 — Локальные зоны

Статус: **real BT A2DP проверен 15.11.2026** (Fosi Audio BT20A PRO), browser-зона
связана с активной зоной; jack 3,5 мм и USB-DAC на железе ещё не проверены.

- [x] PipeWire router (pactl) + mock fallback; jack/USB-DAC/BT zones
- [x] Переключение sink-input без остановки трека
- [x] Per-zone volume/offset
- [x] BT pairing/connect/forget/scan, auto-reconnect hook
- [x] Headless-фикс: `scripts/fix-bluetooth-headless.sh` (WirePlumber seat monitoring)
- [x] UI Zones / Quick Panel

## P8 — Надёжность + сеть (почти закрыто)

- [x] Бэкапы через Backup API, integrity, CLI restore (live restore — нет by design).
- [x] Очистка кэша/maintenance API, timer, soak-скрипт; **24h soak идёт**.
- [x] Wi-Fi API/UI, AP-onboarding helper + open AP test на wlan0 (`/setup` 200);
      телефон владельца — pending.
- [x] Settings API/UI. PIN **удалён владельцем** 15.11; LAN-мутации открыты,
      это residual risk, усиление — ADR-EX-001 (`docs/EXTERNAL_REVIEW_ACTIONS.md`).
- [x] TLS не требуется для v1 (LAN); ADR-035/reverse-proxy — docs.
- [~] 24h soak: прогон **прерван** (3 сэмпла, `interrupted=yes`); процесса нет.
      Повторять после замены БП; текущий статус — `docs/SOAK_24H_REPORT.md`.
      Under-voltage `0x50005` на текущем питании.
- [ ] AP phone test / WPA hotspot (драйвер Pi ключи AP не принял; open AP работает).
- [ ] USB-DAC / physical BT listening — pending оборудования владельца.
- [ ] Внешний блокер: один CDN-edge `ext-strm-…-03` недоступен из сети Pi;
      `CdnRouter` обходит его через соседний edge, но это сетевая эвристика,
      а не гарантия (см. [docs/YANDEX_INVESTIGATION.md](docs/YANDEX_INVESTIGATION.md)).

## P1/P2 hardening — по `docs/EXTERNAL_REVIEW_ACTIONS.md`

- [x] UPL-01: квоты uploads (`upload_quota_bytes`, `upload_min_free_bytes`),
      очистка `.part`, коды `upload_quota_exceeded`/`insufficient_storage`.
- [x] HIST-01: `GET /history`, `DELETE /history`, секция «История» на «Сейчас».
- [x] LIB-01: soft-delete/tombstone (`deleted_at`), restore, удаление файла
      явно, редактирование тегов в БД + best-effort запись в файл.
- [x] PAG-01: «Показать ещё» в поиске и пагинация треков исполнителя.
- [x] PWA-01: manifest + service worker (static shell only, API/stream не кэшируются).
- [x] OBS-01 (частично): `GET /system/runtime` (queue/WS/downloads/mpv/DB),
      карточки в «Состоянии».
- [x] PLY-01: create/rename/delete/add/remove плейлистов Яндекса (real adapter; mock — 503).
- [x] WD-01: watchdog активного sink (15 с), fallback на local; mpv supervisor уже есть.

## P9+ — только по команде

Snapcast, Ynison, DLNA, Chromecast, AirPlay, GPIO, OTA — вне v1.

---

## Отложено сознательно (SPEC §1)

Snapcast/multiroom, Ynison, DLNA/UPnP, Chromecast, AirPlay, голосовое управление,
визуализаторы, нативные приложения, многопользовательскость, тяжёлая обработка
аудио, OTA. Архитектура оставляет для них границы, но код не пишется заранее.
