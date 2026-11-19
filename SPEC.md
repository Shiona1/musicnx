# MusicNX — техническая спецификация v0.2

**Статус:** утверждена к реализации
**Платформа:** Raspberry Pi 3 Model B (1 ГБ RAM), headless, без экрана
**ОС:** Raspberry Pi OS Lite 64-bit
**Хранилище:** SD-карта (ОС, БД, кэш); музыка опционально на USB
**Интерфейс:** Web UI / PWA в браузере клиентов (ПК, телефон, ТВ)
**Подписка:** у владельца активная Яндекс Плюс
**Синхронизация каталога:** автоматически каждый час

---

## 1. Продукт

MusicNX — автономный музыкальный центр и сетевой плеер Яндекс Музыки на Raspberry Pi.
Устройство стоит в LAN (Ethernet и/или Wi-Fi), не имеет экрана и управляется только
через браузер.

Два равноправных режима:
1. **Локальная медиатека** — импорт своих файлов, поиск, очередь, воспроизведение.
   Работает всегда, без интернета и без Яндекса.
2. **Яндекс Музыка** — каталог, плейлисты, «Мне нравится», «Моя волна», потоковое
   воспроизведение, загрузка треков для личного офлайн-использования, тексты песен.

**Главный принцип:** MusicNX Core, локальная медиатека и Player работают независимо
от Яндекс Музыки. Внешняя интеграция расширяет возможности, но не является условием
работоспособности. Это НЕ означает запрет на стриминг: Яндекс — полноценный источник
аудио, а не только каталог метаданных.

### Обязательные возможности v1
- Импорт локальных аудиофайлов, единая медиатека, поиск, избранное, история.
- Каталог Яндекс Музыки: плейлисты, альбомы, исполнители, «Мне нравится».
- Авторизация через OAuth Device Flow (страница настройки с QR/кодом).
- Потоковое воспроизведение треков Яндекса, доступных владельцу подписки.
- «Моя волна» с обратной связью (like / dislike / skip).
- Загрузка треков / альбомов / плейлистов: очередь, теги, обложки, умные папки,
  офлайн-полка. Статус offline только после проверки файла.
- Синхронизированные тексты (LRC, караоке) и plain text fallback.
- Зоны вывода: джек 3.5mm, USB-DAC, Bluetooth A2DP, браузер клиента.
  Переключение без прерывания трека, громкость и offset задержки per-zone.
- Почасовая идемпотентная синхронизация плейлистов.
- Восстановление очереди и состояния после перезагрузки.
- Все клиенты равноправны; сервер — единственный источник истины.

### Сознательно отложено (не делать без отдельной команды)
Snapcast/multiroom, Ynison, DLNA/UPnP, Chromecast, AirPlay, голосовое управление,
визуализаторы, нативные мобильные приложения, многоуровневые пользователи,
тяжёлая обработка аудио, OTA. Архитектура должна позволять добавить их позже.

---

## 2. Платформа и ограничения

| Компонент | Решение |
|---|---|
| Плата | Raspberry Pi 3 Model B, 4 ядра, 1 ГБ RAM |
| ОС | Raspberry Pi OS Lite; 64-bit рекомендуется, 32-bit `armv7l` допускается на Pi 3 (1 ГБ) при условии проверки нативных зависимостей |
| Сеть | Ethernet 100 Мбит/с приоритетнее; Wi-Fi 2.4 ГГц резерв |
| Носитель | SD-карта; музыка опционально USB |
| Backend | Python 3.11+, asyncio, FastAPI |
| БД | SQLite (WAL) |
| Аудиодвижок | mpv (subprocess, JSON IPC) |
| Аудиограф | PipeWire + WirePlumber + pipewire-pulse |
| Bluetooth | BlueZ (A2DP source) |
| Frontend | Svelte + Vite + TypeScript, сборка в статику |
| Обмен состоянием | REST + WebSocket |
| Автозапуск | systemd user service + linger |

### Жёсткие ограничения
1. Не запускать браузерный движок на самой Pi.
2. Не требовать Docker.
3. Не транскодировать аудио нигде (Pi 3 слабый).
4. Не пропускать аудиобайты локального воспроизведения через Python — mpv сам
   открывает URL/файл.
5. Не сканировать всю медиатеку постоянно; импорт по событию/вручную.
6. Не хранить временные аудиофайлы в RAM.
7. Не создавать постоянную запись на SD (см. §11).
8. Не запускать несколько тяжёлых фоновых задач одновременно.
9. Node.js только на dev-машине; на Pi — готовый `dist`.

### Нюанс Pi 3: Wi-Fi + Bluetooth
Wi-Fi и BT делят комбо-чип и антенну 2.4 ГГц. При BT-аудио + активном Wi-Fi возможны
заикания. В UI показывать подсказку «для Bluetooth рекомендуется Ethernet».
Опциональный апгрейд — USB Wi-Fi 5 ГГц.

---

## 3. Зафиксированный стек (не пересматривать)

- Python ≥3.11, asyncio, FastAPI + uvicorn, aiosqlite, httpx/aiohttp, mutagen.
- `yandex-music[async,crypto]` (MarshalX, ≥3.2.2).Extras: `async` — ClientAsync,
  `crypto` — расшифровка encraw. Ynison (`[ynison]`) — только в отложенных фазах.
- mpv: `--idle=yes --no-video --no-terminal --input-ipc-server=<unix socket>`.
  Никаких python-mpv/libmpv.
- PipeWire, WirePlumber, pipewire-pulse; управление через `pactl -f json` / `wpctl`
  (subprocess, аргументы списком, без shell).
- BlueZ: `bluetoothctl` или D-Bus.
- NetworkManager + avahi-daemon (mDNS `<hostname>.local`, дефолт `musicnx.local`).
- Frontend: Svelte + Vite + TypeScript, без тяжёлых UI-библиотек.
- Инструменты: uv (предпочтительно) или venv+pip, ruff, pytest.
- Версии зависимостей пиновать.

---

## 4. Архитектура

```text
        PHONE / PC / TV (браузер, мощный клиент)
                        |
                  HTTP / WebSocket
                        |
                +----------------+
                |  MusicNX UI    |  Svelte / PWA, CardsNX-стиль
                +----------------+
                        |
                  REST / WS / /stream
                        |
        +---------------+----------------+
        |               |                |
  +-----------+   +------------+   +-------------+
  | Core      |   | Sync       |   | Library     |
  | Queue     |   | Engine     |   | Import      |
  | State     |   |            |   | Downloads   |
  +-----------+   +------------+   +-------------+
        |               |                |
        |          Yandex Adapter        |
        |          (каталог/стрим/       |
        |           wave/lyrics)         |
        +---------------+----------------+
                        |
                 SQLite + Storage
                        |
                  mpv (мастер-клок)
                        |
              PipeWire / BlueZ
                        |
        jack / USB-DAC / BT / (browser zone)
```

### Границы модулей
- Yandex Adapter не управляет mpv.
- Player Manager не знает, откуда пришла композиция (локальный файл / URL).
- Core владеет очередью и состоянием; mpv — только исполнитель.
- Весь внешний IO за интерфейсами с mock-реализациями.

### Ключевые инженерные решения
1. **mpv — всегда мастер-клок.** При активной зоне «браузер» локальный вывод mpv
   замьючен (не остановлен), браузер играет `/stream`, синхронизируясь с позицией
   mpv. Допустим двойной трафик в MVP; оптимизация через общий дисковый кэш — позже.
2. **Browser zone:** `GET /api/v1/stream/{track_id}` проксирует оригинальный
   MP3/AAC/FLAC с поддержкой HTTP Range (форвард Range на CDN или отдача локального
   файла с ranges). Без транскодирования. Клиент: `<audio>`, старт с серверной
   позиции, сверка по WS; дрейф > 0.5 с → ресинк.
3. **Позиция:** WS-события 1–2 Гц только активным клиентам + интерполяция на клиенте.
   В SQLite позиция пишется с debounce (см. §11), не каждый тик.
4. **Токены** хранит и обновляет backend; фронтенд их не видит.
5. **Доступность** вычисляется из признаков, а не из одного enum (см. §6).
6. **Ссылки на аудио не хранятся как истина** — получаются перед воспроизведением/
   загрузкой, могут истекать.

---

## 5. Структура репозитория

```text
musicnx/
├── AGENTS.md
├── README.md
├── PLAN.md
├── SPEC.md
├── backend/
│   ├── app/
│   │   ├── main.py  config.py
│   │   ├── api/routes/ (system, player, queue, library, playlists,
│   │   │                search, sync, downloads, zones, settings, stream)
│   │   ├── api/websocket.py
│   │   ├── core/ (player_manager, queue_manager, state_manager, event_bus)
│   │   ├── integrations/yandex_music/ (client, auth, catalog, streaming,
│   │   │                               wave, lyrics, mapper)
│   │   ├── audio/ (router, zones, mpv_ipc, bluetooth)
│   │   ├── sync/ (scheduler, sync_manager, playlist_diff, sync_state)
│   │   ├── library/ (scanner, importer, metadata, covers, storage)
│   │   ├── downloads/ (manager, queue, pipeline, tags)
│   │   ├── lyrics/ (manager, lrc_parser)
│   │   ├── network/ (manager)
│   │   ├── database/ (connection, models, migrations/)
│   │   └── services/ (artwork, health, backup, cache)
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/ (components, layouts, pages, stores, services, styles)
│   ├── public/  package.json  vite.config.ts
├── deployment/ (musicnx.service, install.sh, update.sh, backup.sh,
│                musicnx-maintenance.timer)
├── docs/ (см. §17)
├── config/config.example.yaml
└── .env.example
```

---

## 6. Модель данных (ключевые решения)

Полная схема с колонками и индексами — в `docs/DATABASE.md`. Все изменения схемы —
миграциями. `PRAGMA foreign_keys=ON`, WAL, `synchronous=NORMAL`.

### Правила
- Внешние ID — TEXT в namespace источника. У Яндекса ID трека составной
  (`trackId:albumId`). Уникальность `(source_id, external_id)`.
- Автообъединение только по названию запрещено. Сопоставление: сначала внешний ID,
  затем название+исполнители+альбом+длительность.
- Удаление трека из плейлиста Яндекса не удаляет локальный файл (tombstone/removed_at).
- Доступность — набор признаков, а не один enum:
  `has_local_file`, `has_online_source`, `file_missing`, `source_unavailable`,
  `last_verified_at`. UI-бейдж (offline/online/metadata_only/missing/unavailable)
  вычисляется. Трек может быть одновременно offline и online.
- Одна композиция — одна запись `tracks`; несколько файлов разного качества —
  несколько `media_files`. Плейлисты ссылаются на треки, не копируют файлы.

### Таблицы (ключевые поля)
- `sources` (id, name)
- `artists`, `artist_sources` (artist_id, source_id, external_id)
- `albums` (title, year, cover_path)
- `tracks` (title, album_id, duration_ms, признаки доступности, created/updated)
- `track_sources` (track_id, source_id, external_id, availability, last_checked_at)
- `track_artists` (M2M)
- `media_files` (track_id, path, format, bitrate, size_bytes, duration_ms,
  checksum, verified_at)
- `playlists` (title, source_id, external_id, revision, synced_at)
- `playlist_tracks` (playlist_id, track_id, position, added_at, removed_at);
  уникальность `(playlist_id, position)`; трек может повторяться в плейлисте
- `lyrics` (track_id, language, format plain/lrc, content, source, offset_ms,
  updated_at)
- `play_history` (track_id, played_at, zone_id)
- `playback_state` — singleton (id=1 CHECK): track_id, position_ms, volume,
  status, zone_id, updated_at
- `queue_items` (track_id, position, source_playlist_id, added_at)
- `download_tasks` (track_id, status, priority, attempts, bytes_done, error,
  created/updated)
- `audio_zones` (id, kind local_sink/browser/bt, sink_name, volume,
  delay_offset_ms, last_seen)
- `settings` (key, value)
- `sync_runs` (started/finished, status, added/updated/removed counts, error)
- `schema_migrations` (version, applied_at)

Поиск — SQLite LIKE/FTS5, без отдельного поискового сервера. Списки постранично.

---

## 7. Подсистемы

### 7.1 Core
Состояния: `stopped | playing | paused | buffering | error`.
Владеет очередью, текущим треком, зоной. Сохраняет состояние при изменении и
восстанавливает после перезапуска. Автопродолжение — отдельная настройка.
Передаёт события всем клиентам через event bus → WS.

### 7.2 Yandex Adapter
- Авторизация: `device_auth` (блокирующий → executor). Setup-страница показывает
  verification_url + user_code + QR. Хранение/refresh токенов — наша ответственность
  (файл 0600 вне web root). Запасной вариант: ручной ввод токена.
- Каталог: профиль, плейлисты, состав, треки, альбомы, исполнители. Нормализация
  во внутреннюю модель.
- Стриминг: download_info / tracks_file_info, выбор кодека/качества, обновление
  истекающих ссылок.
- Загрузка: те же источники; encraw расшифровывается библиотекой (`[crypto]`).
- Тексты: tracks_lyrics (синхронный LRC если есть, иначе plain).
- Моя волна: rotor-сессии, feedback like/dislike/skip.
- **Не придумывать методы.** Перед использованием проверять существование в
  установленном пакете (интроспекция) или в docs ym.marshal.dev / OpenAPI
  ym.marshal.dev/api. Расхождения схемы — warning, не падение.
- Без авторизации доступны только 30-секундные превью; UI честно показывает статус.
- Недоступные по региону/правам треки — понятная ошибка, без подмены источниками.

### 7.3 Player (mpv)
Subprocess + JSON IPC через asyncio unix-socket. Подписки: time-pos, duration,
idle-active, eof-reached. Команды: play/pause/stop/seek/next/prev/volume.
Автоперезапуск mpv при падении, восстановление очереди и позиции.
mpv выводит через PipeWire (pipewire-pulse).

### 7.4 Audio Router / Zones
- Sink-и: встроенный джек, USB-DAC (приоритетнее при появлении), BT A2DP.
- Переключение зоны — перенос sink-input mpv на лету без прерывания трека.
- Громкость per-sink, запоминается. Offset задержки per-zone (BT ~150–300 мс)
  применяется к караоке.
- BT: страница паринга (скан/pair/connect), авто-реконнект, забывание устройств.
  Кодек по умолчанию SBC; SBC-XQ опционально.
- Browser zone — виртуальная зона (см. §4). Одна активная зона в MVP.
- Всё управление через subprocess аргументами-списками, без shell.

### 7.5 Library / Import
Импорт MP3/FLAC/AAC/M4A/WAV и др. Сканирование по запросу/событию, не постоянно.
Теги mutagen, обложки из файла или разрешённого источника. Дедупликация по
внешнему ID, затем по признакам. Checksum в фоне, не блокирует импорт.
Обложки кэшировать в 2 размерах (~300px карточки, 1000px Now Playing) +
pre-blurred миниатюра (~64px) для фона. Кэш с LRU-лимитом.

### 7.6 Downloads
- Источник v1: аккаунт пользователя Яндекс Музыки при активной подписке.
- Очередь: 1 параллельная загрузка, пауза 2–5 с между треками (антибан),
  ретраи с backoff, докачка, переживает рестарт.
- Качество из настроек: FLAC → AAC → MP3 320, честный фолбэк + бейдж фактического
  формата. Если FLAC нет — не обманывать.
- encraw расшифровывается библиотекой.
- Раскладка: `Исполнитель/Альбом (Год)/Disc X/NN - Название.ext`;
  `cover.jpg` 1000px в папке альбома; встроенная обложка выбранного размера.
- Теги mutagen (ID3v2 / Vorbis Comments / MP4 ilst): title, artists, albumartist,
  album, year, genre, track/disc number, label.
- Рядом `.lrc` при наличии текста.
- Регистрация в `media_files`, проверка целостности → только потом offline.
- Директория загрузок настраивается (подразумевается USB). Лимит кэша с автоочисткой.

### 7.7 Lyrics
LRC-парсер свой, покрыть тестами. Plain text fallback. Кэш в SQLite + `.lrc` файл.
Караоке: подсветка строки по позиции mpv + offset активной зоны. Нет таймкодов —
обычный текст без синхронизации.

### 7.8 Sync Engine
Режимы: Hourly (3600 с), Manual, On startup. Smart — позже.
Алгоритм: сеть → авторизация → список плейлистов → сравнение ревизий (fallback на
сравнение состава) → fetch изменившихся → нормализация → обновление SQLite →
история → событие в UI.
Правила: не две синхронизации одновременно; при отсутствии сети не менять локальные
данные; при ошибке авторизации сохранить библиотеку и показать предупреждение;
HTTP 429 → backoff; частичные успехи сохранять; следующий запуск по расписанию
независимо от ошибки; ручной запуск не создаёт параллельную задачу.
`last_attempt` и `last_success` хранить отдельно. Идемпотентность обязательна.

### 7.9 Network
NetworkManager: Ethernet приоритетнее (route metric), Wi-Fi резерв. Несколько
Wi-Fi-профилей. avahi → `<hostname>.local` на обоих интерфейсах (дефолт
`musicnx.local`; существующая установка под другим именем допустима).
Страница Wi-Fi и AP-onboarding (`MusicNX-Setup` + страница настройки) — фаза P8.
Failover: WS reconnect с backoff, mpv переживает паузу буфером.

### 7.10 Web
REST + WS + `/stream` + статика SPA. При подключении WS — полный снапшот.
Позиция 1–2 Гц только активным клиентам. Не записывать каждый тик в SQLite.
PWA: service worker кэширует статику/обложки, но не состояние плеера и не приватные
API-ответы бесконтрольно.

---

## 8. API (черновик; точные контракты фиксируются в docs/API.md на P1)

Базовый префикс `/api/v1`. Порт 8080, bind 0.0.0.0 (LAN).

### REST
- system: `GET /system/status|health|storage`
- auth: `POST /auth/device/start`, `GET /auth/device/status`, `POST /auth/token`,
  `POST /auth/logout`
- player: `GET /player/state`, `POST /player/play|pause|stop|next|previous|seek|volume`
- queue: `GET /queue`, `POST /queue`, `DELETE /queue/{id}`, `POST /queue/reorder`
- library: `GET /library/tracks|albums|artists|search`, `POST /library/import`
- playlists: `GET /playlists`, `GET /playlists/{id}`, `POST /playlists/{id}/play`,
  `POST /playlists/{id}/sync`
- yandex: `GET /yandex/likes`, `GET /yandex/album/{id}`, `GET /yandex/artist/{id}`,
  `GET /yandex/search`, `POST /yandex/wave/start`, `POST /yandex/wave/feedback`
- downloads: `GET /downloads`, `POST /downloads`, `DELETE /downloads/{id}`
- zones: `GET /zones`, `POST /zones/{id}/activate`, `POST /zones/{id}/volume`,
  `POST /zones/{id}/offset`
- bluetooth: `GET /bluetooth/devices`, `POST /bluetooth/scan|pair|connect|forget`
- lyrics: `GET /lyrics/{track_id}`
- sync: `GET /sync/status|history`, `POST /sync/start|cancel`
- settings: `GET /settings`, `PUT /settings`
- stream: `GET /stream/{track_id}` (Range обязателен)

### WebSocket `/api/v1/ws`
Конверт `{type, ts, payload}`. При подключении — снапшот.
События: `player.state_changed`, `player.track_changed`, `player.position`,
`queue.updated`, `zone.changed`, `volume.changed`, `lyrics.updated`,
`download.progress`, `download.done`, `sync.started|progress|completed|failed`,
`library.updated`, `bt.device_*`, `network.status`, `auth.required`,
`system.warning`.

---

## 9. Web UI

Визуальный ориентир — CardsNX / HomeNX, адаптированный под браузер.
Тёмная тема, большие карточки обложек на матовом стекле, горизонтальные полки,
крупный Now Playing с размытым фоном из обложки, выдвижная быстрая панель.
Эффекты (blur, анимации) выполняет клиент; сервер отдаёт готовые обложки и статику.
Анимации только transform/opacity. Responsive: ПК + телефон + ТВ.
Для слабых ТВ-браузеров — настройка «облегчённый режим» (без blur).

### Экраны
- **Setup** — Device Flow + QR, ручной токен fallback.
- **Home** — полки: Мне нравится, Плейлисты, Волна, Загрузки, Недавнее;
  большая карточка текущего трека.
- **Library** — треки/исполнители/альбомы/локальные файлы, фильтры доступности.
- **Playlist / Album** — список треков, «Скачать всё», «Воспроизвести».
- **Now Playing** — крупная обложка, прогресс, очередь, громкость, караоке.
- **Search** — локальный + Яндекс.
- **Downloads** — очередь, прогресс, ошибки.
- **Sync Center** — последняя/следующая синхронизация, counts, ошибки, ручной запуск.
- **Zones / Quick Panel** — громкость, селектор зоны, BT/Wi-Fi статус, таймер сна,
  перезагрузка/выключение.
- **Settings** — авторизация, интервал синхронизации, папка медиатеки, качество,
  размер обложек, offset-ы зон, аудио, хранилище, бэкап, безопасность, legal.

---

## 10. Безопасность

- Web UI в LAN, не публиковать в интернет по умолчанию.
- Авторизация для изменения настроек/синхронизации/импорта: PIN/пароль,
  сессия через HttpOnly + SameSite cookie.
- Проверка Origin/Host (защита от DNS rebinding). CORS не считать авторизацией.
- Токены Яндекса — только backend, файл 0600, не в логах, не через WS.
- Проверять пути импорта (path traversal).
- Не выполнять shell из пользовательских параметров; subprocess аргументами-списками.
- Секреты не логировать.

---

## 11. Надёжность SD-карты (обязательное требование)

1. SQLite WAL, `synchronous=NORMAL`, `foreign_keys=ON`.
2. Позиция плеера — debounce 5–10 с + при pause/stop/track change/shutdown.
   Не писать каждый тик.
3. Метаданные обновлять пакетами.
4. Ротация логов с ограничением размера.
5. Кэш обложек/метаданных с LRU-лимитом.
6. Очистка временных файлов.
7. Бэкап БД через SQLite Backup API на другой носитель (не копированием активного
   файла вслепую). Процедура восстановления.
8. Проверка целостности БД при обслуживании.
9. Корректное закрытие SQLite при остановке.
10. `noatime` для носителя (рекомендация в install).
11. Не сканировать библиотеку постоянно.
12. Восстановление состояния после неожиданной перезагрузки.

---

## 12. Развёртывание

- systemd **user** service `musicnx.service` под параметризованным пользователем
  (дефолт `musicnx`; существующая установка под другим именем допустима)
  + `loginctl enable-linger` (PipeWire — user-сервисы без логина).
- mpv — дочерний процесс backend в MVP; отдельный юнит не обязателен.
- `musicnx-maintenance.timer` — очистка кэша, бэкап.
- `install.sh` повторно запускаемый, не разрушает библиотеку:
  проверка ОС/архитектуры → apt (mpv, pipewire, pipewire-pulse, wireplumber,
  bluez, avahi-daemon, network-manager) → пользователь → каталоги → venv →
  готовый frontend dist (Node на Pi не нужен) → units → linger → health check →
  вывод адреса `http://<hostname>.local:8080` (hostname параметризован, дефолт
  `musicnx`; не хардкодить `musicnx.local` в коде и установке).
- `update.sh`, `backup.sh`.
- Каталоги: `/opt/musicnx` (app, venv, frontend), `/var/lib/musicnx`
  (database, library, cache, downloads, backups), `/var/log/musicnx` (опционально).
- Пути библиотеки/кэша/загрузок — в конфиге, чтобы перенести на USB без изменения кода.
- Рекомендация в README: `gpu_mem=16`, Ethernet для BT.

---

## 13. Этапы разработки

Каждый этап заканчивается рабочим состоянием, тестами и отчётом.

- **P0 — Bootstrap.** Репозиторий, весь набор docs (§17), AGENTS.md, каркас
  backend/frontend, mock-адаптеры, конфиг, логирование, event bus, базовые миграции,
  ruff+pytest, dev-команды. Чек-лист железа — `docs/HARDWARE.md` (выполняет человек).
- **P1 — Core + минимальный Web UI.** mpv IPC, очередь, state machine, сохранение/
  восстановление, WS-шина, REST, минимальный UI (полка, play/pause/seek/next,
  очередь). Mock e2e без токена и без Pi; real — локальный тестовый трек.
  Синхронизация двух вкладок. Тесты: очередь, state machine, шаблон путей,
  вычисление доступности.
- **P2 — Media Library.** Импорт, сканирование, теги, обложки, дедупликация,
  поиск, признаки доступности, миграции.
- **P3 — Yandex: auth + каталог + стриминг + Волна.** Device Flow, плейлисты,
  Мне нравится, получение аудиоисточника, онлайн-воспроизведение, browser zone
  (`/stream` + Range), Моя волна с feedback.
- **P4 — Downloads + офлайн.** Полный пайплайн §7.6, страница загрузок,
  офлайн-полка, переживает рестарт.
- **P5 — Lyrics / караоке.** LRC parser, кэш, подсветка, offset.
- **P6 — Sync Engine.** Hourly scheduler, diff, идемпотентность, история,
  защита от дублей, backoff.
- **P7 — Локальные зоны.** PipeWire router, джек/USB-DAC, BT паринг/реконнект,
  бесшовное переключение, per-zone громкость/offset.
- **P8 — Надёжность + сеть.** Бэкапы, очистка кэша, длительные тесты,
  страница Wi-Fi, AP-onboarding, failover-полировка.
- **P9+ — только по команде:** Snapcast, Ynison, GPIO, OTA и пр.

---

## 14. Критерии приёмки v1

- [ ] Pi запускает MusicNX без рабочего стола и без браузера на плате.
- [ ] Web UI доступен с другого устройства в LAN.
- [ ] Импорт локальных файлов, поиск, очередь, управление работают.
- [ ] mpv воспроизводит локальные файлы.
- [ ] Состояние восстанавливается после перезагрузки.
- [ ] Device Flow авторизация работает; токены обновляются.
- [ ] Онлайн-трек Яндекса воспроизводится (при подписке).
- [ ] Моя волна запускается, feedback влияет на подборку.
- [ ] Трек/альбом/плейлист скачивается, файл проверен, статус offline честный.
- [ ] Удаление трека из внешнего плейлиста не удаляет локальный файл.
- [ ] Повторная синхронизация не создаёт дубликатов; синхронизация раз в час.
- [ ] Ошибка сети не повреждает библиотеку.
- [ ] UI показывает доступность каждой композиции.
- [ ] Тексты/LRC отображаются, караоке синхронно с offset зоны.
- [ ] Зоны: джек / USB-DAC / BT / браузер; переключение без прерывания.
- [ ] Звук в браузере: Range-перемотка работает.
- [ ] Система выдерживает длительную работу без утечки памяти и роста логов.
- [ ] Реальные измерения RAM/CPU/температуры на Pi 3 проведены.

---

## 15. Риски

| Риск | Мера |
|---|---|
| Изменение API Яндекса | Изолированный Adapter, проверка методов, тесты |
| Потеря авторизации | Отдельный статус, refresh, повторное подключение |
| Rate limits / бан | Backoff, задержки в очередях, кэш |
| Износ SD | WAL, debounce, ротация, бэкапы |
| Нехватка RAM | Минимум процессов, измерения на Pi |
| Повреждение БД | Транзакции, миграции, Backup API |
| Недоступность источника | Честные статусы, локальная медиатека |
| Сбой mpv | Контроль процесса, восстановление |
| Wi-Fi + BT coexistence | Подсказка Ethernet, опциональный USB Wi-Fi |
| Истечение ссылок | Получать перед использованием, не хранить как истину |

---

## 16. Правовая рамка

- Неофициальный API, личное использование при активной подписке Плюс.
- Скачанное не распространять.
---

## 17. Набор документов (создаётся на P0)

- `AGENTS.md` — правила для агентов (см. промт).
- `README.md` — продукт, quickstart dev/mock, деплой, legal.
- `PLAN.md` — фазы со статусами.
- `SPEC.md` — этот документ, источник истины.
- `docs/ARCHITECTURE.md` — модули, границы, потоки данных.
- `docs/DATABASE.md` — полная схема, индексы, pragma, миграции.
- `docs/API.md` — точные REST/WS контракты.
- `docs/AUDIO.md` — mpv IPC, PipeWire, зоны, browser sync, offsets.
- `docs/SYNC.md` — алгоритм и правила синхронизации.
- `docs/DOWNLOADS.md` — пайплайн, очередь, форматы, теги, папки.
- `docs/UI.md` — экраны, CardsNX-стиль, компоненты, responsive.
- `docs/SECURITY.md` — модель защиты LAN.
- `docs/DEPLOYMENT.md` — install, systemd, linger, сеть.
- `docs/HARDWARE.md` — чек-лист проверки Pi (выполняет человек).
- `docs/DECISIONS.md` — журнал архитектурных решений (ADR).
- `docs/ROADMAP.md` — фазы + отложенное.
- `docs/LEGAL.md` — правовая рамка.
- `docs/TESTING.md` — стратегия тестов, mock-режим.

Все документы с реальным содержанием, без заглушек «TODO». Противоречия с SPEC
фиксируются в DECISIONS.md; SPEC имеет приоритет.