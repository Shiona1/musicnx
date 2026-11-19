# API MusicNX

Базовый префикс — `/api/v1`, порт 8080, bind `0.0.0.0` (LAN). Источник
требований — [SPEC.md](../SPEC.md) §8; здесь зафиксированы точные контракты.
Статус каждого раздела отражает реальность P1: нереализованных маршрутов в
приложении **нет**, они появятся в своих фазах.

- Формат — JSON (`application/json; charset=utf-8`), имена полей snake_case.
- Время — RFC3339 UTC с миллисекундами: `2026-11-14T12:00:00.000Z`.
- MUSICNX не поддерживает CORS-авторизацию; `Origin` проверяется (docs/SECURITY.md).

## 0. Общие типы

### TrackDTO

```json
{
  "id": 12,
  "title": "Ночной эфир",
  "artists": [{"id": 3, "name": "Тестовый Оркестр"}],
  "album": {"id": 2, "title": "Mock Album", "year": 2024, "cover_path": null},
  "duration_ms": 214000,
  "availability": "online",
  "has_local_file": false,
  "has_online_source": true,
  "file_missing": false,
  "source_unavailable": false,
  "genre": "Electronic",
  "label": null,
  "cover_url": "/api/v1/artwork/ab12cd34ef567890ab12cd34ef567890?size=300"
}
```

`availability` ∈ `offline | online | metadata_only | missing | unavailable`
(приоритет — docs/DATABASE.md §4.2). `cover_url` — относительный URL готовой
обложки 300px или `null` (фронтенд может запросить `size=1000`/`64`).

### PlayerState

```json
{
  "status": "playing",
  "track": { "...": "TrackDTO или null" },
  "position_ms": 42000,
  "duration_ms": 214000,
  "volume": 70,
  "zone_id": "local",
  "queue_index": 0,
  "queue_item_id": 5,
  "can_play": false,
  "can_pause": true,
  "can_next": true,
  "can_previous": true,
  "updated_at": "2026-11-14T12:00:00.000Z"
}
```

`status` ∈ `stopped | playing | paused | buffering | error`.
`can_*` — вычисленные подсказки для UI (не команды).

### QueueDTO

```json
{
  "items": [
    {"id": 5, "position": 0, "source_playlist_id": null, "track": {"...": "TrackDTO"}}
  ],
  "current_item_id": 5,
  "current_index": 0,
  "total": 1,
  "total_duration_ms": 214000
}
```

### Ошибка

```json
{"error": {"code": "not_found", "message": "Track not found", "details": {"track_id": 99}}}
```

| HTTP | code |
|---|---|
| 400 | `bad_request`, `bad_message` |
| 403 | `origin_forbidden`, `host_forbidden` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `conflict`, `no_playable_source`, `queue_changed` |
| 422 | `validation_error` |
| 429 | `too_many_attempts` |
| 500 | `internal_error` |
| 503 | `unavailable` |

## 1. System — реализовано (P0/P1)

### `GET /api/v1/system/health`

```json
{"status": "ok", "version": "0.2.0", "mode": "mock", "uptime_s": 12.4, "db": "ok"}
```

`db: "error"` и HTTP 503, если `SELECT 1` не выполняется. Используется
health-check при деплое.

### `GET /api/v1/system/status`

```json
{
  "version": "0.2.0",
  "mode": "mock",
  "started_at": "2026-11-14T12:00:00.000Z",
  "uptime_s": 12.4,
  "player": {"...": "PlayerState"},
  "audio": {"backend": "mock", "ready": true},
  "database": {"path": "/abs/var/musicnx.db", "size_bytes": 245760, "schema_version": 3},
  "adapters": {
    "yandex": {"backend": "mock", "available": false, "authenticated": false, "note": "P3"},
    "router": {"backend": "mock", "outputs": [{"id": "mock-local", "kind": "local_sink", "name": "Mock output"}]},
    "bluetooth": {"backend": "mock", "adapter": "off", "devices": 0},
    "network": {"backend": "mock", "connected": true, "interface": "mock0"}
  },
  "security": {"auth": "trusted_lan", "pin_configured": false}
}
```

`security.auth` — **legacy field**: фактически PIN удалён, `require_mutation_access`
no-op; значение `"pin"`/`"localhost_only"` не является действующей защитой.
`pin_configured` — только legacy-флаг из старого конфига. Актуальная модель — LAN
trusted, см. §4 и ADR-EX-001.

### `GET /api/v1/system/storage`

```json
{
  "paths": {
    "data": "/abs/var",
    "database": "/abs/var/musicnx.db",
    "library": "/abs/var/library",
    "downloads": "/abs/var/downloads",
    "cache": "/abs/var/cache"
  },
  "exists": {"database": true, "library": false, "downloads": false, "cache": true},
  "database_size_bytes": 245760,
  "disk": {"total_bytes": 31914983424, "used_bytes": 12000000000, "free_bytes": 19914983424}
}
```

`disk` — по носителю каталога данных (`shutil.disk_usage`, вне event loop).

### `GET /api/v1/system/metrics`

```json
{
  "version": 1,
  "interval_s": 5,
  "minutes": 60,
  "samples": [
    {
      "ts": "2026-11-15T21:00:00+00:00",
      "uptime_s": 12345.6,
      "cpu_pct": 7.2,
      "load1": 0.42,
      "mem_total_kb": 992848,
      "mem_used_kb": 215804,
      "mem_pct": 21.7,
      "temp_c": 41.9,
      "clock_mhz": 1200,
      "throttled": "0x50005",
      "disk_free_bytes": 113936818176
    }
  ]
}
```

Кольцевой буфер в памяти (до 720 сэмплов, интервал 5 с); БД не пишется.
`minutes` — окно 1..1440. `null` означает, что источник недоступен (не Linux,
нет `vcgencmd`/sysfs), а не ноль.

### `GET /api/v1/system/runtime` (open)
Runtime counters без секретов: `uptime_s`, `ws_clients`, `queue_depth`,
`downloads {total, by_status}`, `database.size_bytes`,
`audio {ready, restarts, error}`, `security.auth`. Дополнение к метрикам для
watchdog/диагностики; не заменяет `/system/status`.

### `GET /api/v1/system/share`
Адрес веб-интерфейса для других устройств LAN + QR-код (PNG data-URI) для
сканирования телефоном. Основа — заголовок `Host` текущего клиента (то, чем
браузер уже открыл интерфейс); loopback-хост подменяется LAN-адресом сервера
(UDP-трюк без отправки пакетов), флаг `loopback: true` честно означает, что
даже этот адрес с телефона недоступен. `qr_data_uri: null` — QR не
отрисовался (UI показывает только ссылку). `200 SystemShare`.

### `POST /api/v1/system/reboot` · `POST /api/v1/system/shutdown`

Требуют `{"confirm": true}`; без него — `422 confirmation_required`. Сервер
проверяет passwordless sudo (`sudo -n true`), планирует `systemctl reboot` /
`systemctl poweroff` через 2 с и сразу отвечает `202`:

```json
{"status": "reboot_scheduled", "delay_s": 2}
```

`503 unavailable`, если passwordless sudo не настроен. Это осознанно
разрушающее действие: UI обязан показать подтверждение.


## 2. Player — реализовано (P1)

Все изменяющие команды возвращают актуальный `PlayerState` и порождают
WS-события всем клиентам.

### `GET /api/v1/player/state`
`200 PlayerState`.

### `POST /api/v1/player/play`

Тело необязательно:

```json
{"track_id": 12}
```

- без тела — продолжить с паузы; если ничего не выбрано — первый элемент очереди;
  если очередь пуста — `409 conflict`;
- `track_id` есть в очереди — играть его (первое вхождение);
- `track_id` отсутствует в очереди — добавить в конец и играть;
- неизвестный `track_id` — `404`;
- в real-режиме нет playable-файла — `409 no_playable_source`.

`200 PlayerState` (статус `playing`).

### `POST /api/v1/player/pause`
`200 PlayerState` (статус `paused`; если уже пауза/стоп — идемпотентно).

### `POST /api/v1/player/stop`
Останавливает, позиция 0, статус `stopped`. `200 PlayerState`.

### `POST /api/v1/player/next`
Следующий элемент очереди; если его нет — `stopped` на конце. `200 PlayerState`.

### `POST /api/v1/player/previous`
Если позиция > 3000 мс — перемотка в 0 (ADR-011); иначе предыдущий элемент;
если его нет — в 0. `200 PlayerState`.

### `POST /api/v1/player/seek`

```json
{"position_ms": 30000}
```

`position_ms` ≥ 0 (int); значение больше длительности зажимается до
`duration_ms`. `422` при отрицательном/не-int. `200 PlayerState`.

### `POST /api/v1/player/volume`

```json
{"volume": 65}
```

Целое 0..100 (ADR-010). Сохраняется в `settings` (`player.volume`) и в
`playback_state`. `422` вне диапазона. `200 PlayerState`.

### `POST /api/v1/player/mode`

```json
{"shuffle": true}
{"repeat": "all"}
```

Режимы воспроизведения. Хотя бы одно поле обязательно (`422` иначе); незаданное
поле не меняется. `repeat` — `"off" | "one" | "all"`.

- `shuffle: true` — автопереход и кнопка Next выбирают случайный playable-элемент
  (текущий не повторяется при 2+ элементах); Previous всегда идёт по порядку очереди;
- `repeat: "one"` — по концу трека текущий элемент запускается заново (без записи
  в историю и без события `track_changed`); ручной Next при этом переходит дальше;
- `repeat: "all"` — по концу очередь заворачивается на первый элемент; ручной Next
  на последнем элементе тоже заворачивается.

Оба режима сохраняются в `settings` (`player.shuffle`, `player.repeat`) и
переживают рестарт. `can_next` учитывает режимы (последний элемент + repeat all
или shuffle = `true`). `200 PlayerState` (поля `shuffle`, `repeat`).

## 3. Queue — реализовано (P1)

### `GET /api/v1/queue`
`200 QueueDTO`.

### `POST /api/v1/queue`

```json
{"track_ids": [12, 13, 12]}
```

Добавляет в конец в указанном порядке; повторения разрешены; пустой список — 422;
неизвестный трек — 404 (ничего не добавляется, транзакция атомарна).
`201 QueueDTO`.

### `DELETE /api/v1/queue/{item_id}`
Удаляет элемент, перенумеровывает позиции. Если удалён текущий элемент —
воспроизведение останавливается, выбирается следующий по позиции (или состояние
`stopped`, если очередь пуста). `200 QueueDTO`, `404` если нет.

### `POST /api/v1/queue/reorder`

```json
{"item_ids": [7, 5, 6]}
```

`item_ids` — полная перестановка текущих элементов (тот же набор, без повторов).
Иначе `409 queue_changed` с `details.expected`. `200 QueueDTO`.
Текущий элемент сохраняется (меняется только индекс).

### `DELETE /api/v1/queue`
Убирает все будущие элементы; текущий продолжает играть (Spotify-семантика).
Если ничего не играет — очередь очищается целиком. `200 QueueDTO`.

## 4. Auth — legacy, PIN removed

**Актуально на 2026-11-15:** PIN удалён владельцем (docs/PIN_REMOVAL.md).
`require_mutation_access` — no-op: mutation-маршруты открыты в доверенной LAN.
Host/Origin по-прежнему проверяются. Не выставлять сервис в интернет.

Legacy-маршруты `GET /auth/status`, `POST /auth/pin`, `POST /auth/pin/change`,
`POST /auth/logout` могут присутствовать для совместимости, но **не являются
действующей защитой** и не должны использоваться новыми клиентами. Усиление
доступа — ADR-EX-001 в docs/EXTERNAL_REVIEW_ACTIONS.md, требует решения
владельца. Read-only player/queue/library/stream открыты.

## 5. Library — реализовано (P2)

### `GET /api/v1/library/tracks`

Query: `limit` (1..200, default 50), `offset` (≥0), `search` (FTS5 по title/
artists/album), `availability` (`all` default | один из бейджей).

```json
{"items": [{"...": "TrackDTO"}], "total": 9, "limit": 50, "offset": 0}
```

### `DELETE /api/v1/library/tracks/{track_id}` — mutation (PIN removed; LAN open)

По умолчанию ставит tombstone (`tracks.deleted_at`), убирает трек из
библиотеки/очереди/поиска и помечает playlist composition удалённой; **файл и
история остаются**. `delete_file=true` — явное удаление локального файла только
внутри `library.import_roots` и только если на путь не ссылается другой трек.
Ответ: `{"deleted": true, "soft_deleted": true, "files_deleted": []}`;
неизвестный id — `404`. Это destructive-операция, UI требует подтверждения.

### `POST /api/v1/library/tracks/{track_id}/restore` — mutation
Снимает tombstone (`deleted_at=NULL`), возвращает `Track` и переиндексирует FTS.
Если трек не был удалён — `404`.

### `PATCH /api/v1/library/tracks/{track_id}` — mutation
Меняет метаданные в БД и best-effort переписывает теги локального файла:
`title`, `artists[]`, `album`, `year`, `genre`, `track_number`, `disc_number`.
Ответ: `{"track": TrackDTO, "file_tags": "written|skipped|failed", "warning": …}`.
Пустое тело → `422`; неизвестный/удалённый трек → `404`.

### `GET /api/v1/library/albums`

Query: `limit`, `offset`, `search` (LIKE по альбому/исполнителю).

```json
{
  "items": [{
    "id": 2, "title": "Test Album", "year": 2024,
    "artist": {"id": 1, "name": "Test Artist"},
    "cover_url": "/api/v1/artwork/ab12cd34ef567890ab12cd34ef567890?size=300",
    "track_count": 2
  }],
  "total": 4, "limit": 50, "offset": 0
}
```

### `GET /api/v1/library/artists`

Query: `limit`, `offset`, `search`. Поля: `id`, `name`, `cover_url`,
`track_count`, `album_count`.

### `GET /api/v1/library/albums/{album_id}`
Детальная страница альбома: `album` (та же схема, что в списке) + `tracks` —
треки альбома по `disc_number`, `track_number`, затем по названию.
`200 AlbumDetail`, `404` если альбома нет.

### `GET /api/v1/library/artists/{artist_id}`
Детальная страница исполнителя: `artist` + `tracks` (все живые треки,
где он указан, по названию). `200 ArtistDetail`, `404` если нет.

### `GET /api/v1/library/search`

Query: `q` (обязателен, 1..200), `limit` 1..100 (default 25), `offset`.

```json
{
  "tracks": {"items": [], "total": 2, "limit": 25, "offset": 0},
  "albums": {"items": [], "total": 0, "limit": 25, "offset": 0},
  "artists": {"items": [], "total": 0, "limit": 25, "offset": 0}
}
```

### `POST /api/v1/library/upload` — **mutation (PIN removed; LAN open)**

`multipart/form-data` с одним полем `file` (аудиофайл). Ответ `201`:

```json
{"file": {"name": "track.mp3", "size": 1234567, "path": "/home/musicnx/musicnx/media/uploads/track.mp3"}}
```

Файл сохраняется в `<первый import root>/uploads`, имя санитизируется
(basename, управляющие символы, уникальный суффикс), расширение обязано быть
в `library.extensions`, размер ограничен `library.upload_max_bytes`
(по умолчанию 300 МБ). Дополнительно действуют `library.upload_quota_bytes`
(общий размер uploads, 0 = без лимита) и `library.upload_min_free_bytes`
(резерв свободного места); превышение — `413 upload_quota_exceeded` или
`507 insufficient_storage`, частичный файл удаляется. `path` затем передаётся
в `POST /library/import` (`{"paths": ["…"]}`) — импорт отдельный, потому что
он single-flight.
Ошибки: `400 empty_file`, `413 file_too_large`, `415 unsupported_media_type`.

### `POST /api/v1/library/import` — **mutation (PIN removed; LAN open)**

Тело необязательно:

```json
{"paths": ["/home/musicnx/musicnx/media"]}
```

Без `paths` сканируются `library.import_roots` (или `paths.library`). Пути
обязаны находиться внутри разрешённых корней; иначе `403 path_not_allowed`.
Ответ `202` — job:

```json
{
  "id": "f3c1…", "status": "queued", "phase": "queued",
  "total": 0, "processed": 0, "imported": 0, "updated": 0, "skipped": 0,
  "missing": 0, "verify_total": 0, "verify_processed": 0, "percent": 0,
  "errors": [], "roots": ["/home/musicnx/musicnx/media"],
  "target_paths": [], "started_at": "…", "finished_at": null,
  "cancel_requested": false
}
```

`status`: `queued | running | verifying | cancelling | completed | failed |
cancelled`; `phase`: `discovering | importing | verifying | done | failed |
cancelled`. Один импорт одновременно; повторный запуск — `409 import_running`.
Ошибки отдельных файлов не прерывают job (последние 20 в `errors`).

### `GET /api/v1/library/import/status`

`{"job": <job|null>}` — последний/текущий job.

### `POST /api/v1/library/import/cancel` — **mutation (PIN removed; LAN open)**

Ставит `cancel_requested`; без активного job — `409 import_not_running`.

### `GET /api/v1/artwork/{key}`

Query `size` ∈ `64|300|1000` (default 300). Отдаёт JPEG из кэша с
`Cache-Control: public, max-age=31536000, immutable`; неизвестный ключ/размер —
`404`/`422`. Ключ — 32 hex-символа, вычисляется импортом.

## 6. Yandex Music — P3

Все методы `yandex_music` — только из [docs/YANDEX_API.md](YANDEX_API.md).
Токены никогда не возвращаются: ни в REST, ни в WS, ни в `/system/status`.
`mode=mock` использует mock-адаптер (без сети); `mode=real` — реальный токен.
Mock auth: `device/start` → `pending`, второй `device/status` → `authorized`
(login `mock-user`); manual token принимает любое непустое значение. Это явно
mock, не имитация реального Яндекса.

### 6.1 Auth

#### `POST /api/v1/yandex/auth/device/start` — mutation (PIN removed; LAN open)

Запускает Device Flow (`request_device_code` + фоновый poll). Тело пустое.

```json
{
  "flow_id": "8f3a…",
  "user_code": "ABCD-EFGH",
  "verification_url": "https://ya.ru/device",
  "expires_in": 300,
  "interval": 5,
  "qr_data_uri": "data:image/png;base64,…"
}
```

`qr_data_uri` — QR на `verification_url` (не секрет). `device_code` хранится
только на сервере.

#### `GET /api/v1/yandex/auth/device/status?flow_id=…`

```json
{"state":"pending|authorized|expired|denied|error","flow_id":"…","login":null,"expires_at":null,"error":null}
```

Состояние `authorized` означает, что токен уже сохранён; WS `auth.status`.

#### `POST /api/v1/yandex/auth/token` — mutation (PIN removed; LAN open)

```json
{"token": "y0_..."}
```

Валидирует токен через `ClientAsync.init()`/`account_status`; сохраняет
`access_token` (manual, без refresh) в файл 0600. `200 YandexAuthStatus`.
Неверный токен — `401 invalid_token`.

#### `POST /api/v1/yandex/auth/logout` — mutation (PIN removed; LAN open)

Удаляет токен/refresh, очищает клиент, WS `auth.status`.

#### `GET /api/v1/yandex/auth/status`

```json
{"authenticated": true, "login": "user@example", "expires_at": "2026-11-21T…Z", "source": "device"}
```

Без токенов. Если не авторизован — `authenticated:false`, остальные поля null.

### 6.2 Catalog

`TrackDTO` расширяется полями P3:
`source` (`"yandex"`), `external_id` (`"<track_id>:<album_id>"`), `preview`
(bool, 30-секундный превью), `liked` (bool|null), `cover_url` (URL обложки
Yandex или `/artwork/...`). Существующие P1/P2 поля сохраняются.

#### `GET /api/v1/yandex/likes?limit=50&offset=0`
`200 LibraryPage` (треки «Мне нравится», внутренние track_id).

#### `GET /api/v1/yandex/playlists`

```json
{"items":[{"id":"123:456","kind":456,"title":"Плейлист","track_count":12,"cover_url":"…","owner":"user"}],"total":1}
```

#### `GET /api/v1/yandex/playlists/{playlist_id}?limit=50&offset=0`

`playlist_id` — `"<uid>:<kind>"` (owner + kind). Ответ:
`{"playlist": YandexPlaylist, "tracks": LibraryPage}`.

#### `POST /api/v1/yandex/playlists` — mutation
`{"title":"…","visibility":"private|public"}` → `201 YandexPlaylist`.
#### `PATCH /api/v1/yandex/playlists/{id}` — mutation
`{"title":"…"}` → `YandexPlaylist`.
#### `DELETE /api/v1/yandex/playlists/{id}` — mutation
`204`; удаляет плейлист в аккаунте (не локальные файлы).
#### `POST /api/v1/yandex/playlists/{id}/tracks` — mutation
`{"track_id":5,"at":0}` → обновлённый `YandexPlaylist`; трек должен иметь
Yandex source (`trackId:albumId`).
#### `DELETE /api/v1/yandex/playlists/{id}/tracks?position=N` — mutation
Убирает трек по 0-based позиции; возвращает `YandexPlaylist`.

В mock-режиме редактирование недоступно (`503
yandex_playlist_editing_unavailable`); GET-каталог работает.

#### `GET /api/v1/yandex/album/{album_id}`
`{"album": AlbumSummary, "tracks": [TrackDTO]}` (`albums_with_tracks`).

#### `GET /api/v1/yandex/artist/{artist_id}`
`{"artist": ArtistSummary, "tracks": [TrackDTO]}` (`artists_tracks`).

#### `GET /api/v1/yandex/artist/{artist_id}/tracks?limit=25&offset=0`
`LibraryPage` с пагинацией треков исполнителя; используется кнопкой
«Показать ещё» на странице исполнителя.

#### `GET /api/v1/yandex/search?q=…&type=all|tracks|albums|artists|playlists&limit=25&offset=0`

```json
{
  "tracks": {"items":[],"total":0,"limit":25,"offset":0},
  "albums": {"items":[],"total":0,"limit":25,"offset":0},
  "artists": {"items":[],"total":0,"limit":25,"offset":0},
  "playlists": {"items":[],"total":0}
}
```

#### `GET /api/v1/yandex/cover?uri=…&size=300`

Прокси/кэш обложки Yandex (`avatars.yandex.net` only; `size` 64/300/1000).
UI всегда ходит через этот endpoint; backend складывает картинку в общий
`CoverCache` (LRU) и отдаёт `/api/v1/artwork/{key}` (307), поэтому direct URL
в браузер не попадает.

### 6.3 Wave

#### `POST /api/v1/yandex/wave/start`

Открытый (не mutation): запускает станцию `user:onyourwave` через
`rotor_station_tracks`, нормализует треки в БД.

```json
{"station":"user:onyourwave","batch_id":"…","session_id":null,"tracks":[TrackDTO],"current_index":0,"updated_at":"…"}
```

#### `POST /api/v1/yandex/wave/feedback`

```json
{"action":"like|dislike|skip","track_id":123,"played_seconds":42}
```

- `skip` → `rotor_station_feedback(station, "skip", track_id=…,
  total_played_seconds=…)`;
- `like`/`dislike` → `users_likes_tracks_add` / `users_dislikes_tracks_add`
  (подтверждённые методы; см. YANDEX_API §4), т.к. документированные типы
  station feedback не включают like/dislike.
- Ответ — обновлённый `WaveState`; WS `wave.updated`. Refill — если треков
  осталось мало, вызывается `rotor_station_tracks(queue=last_id)`.

### 6.4 Stream

#### `GET /api/v1/stream/{track_id}`

- Локальный файл — Range по файлу; Yandex — Range-прокси на direct link.
- `Accept-Ranges: bytes`; запрос с `Range` → `206` + `Content-Range`;
  без Range → `200` (файл/поток, но не буферизовать целиком в RAM).
- `Content-Type` — из upstream/файла; никакого транскодирования.
- Ссылка истекает → получить заново; ошибка → `503 stream_unavailable`.
- Токены/подписанные URL не логируются; браузер получает только поток.
- Для `preview=true` отдаётся превью как есть; UI показывает «30 сек».

### 6.5 Browser zone

`audio.browser_zone=true`: mpv остаётся мастер-клоком, его локальный вывод
muted; `<audio>` клиента играет `/stream/{track_id}`, синхронизируется по
`player.position` (дрейф >0.5 с → ресинк). При `false` поведение P1/P2.
`audio.browser_zone` виден в `/system/status` (`audio.browser_zone`).

## 7. WebSocket — реализовано (P1/P2)

`GET /api/v1/ws` (Upgrade). Перед `accept` проверяются Origin и Host; при
нарушении — close `1008` (policy violation). При подключении сервер сразу
отправляет снапшот.

Конверт всех сообщений сервера:

```json
{"type": "player.state_changed", "ts": "2026-11-14T12:00:00.123Z", "payload": {}}
```

### Снапшот (первое сообщение)

```json
{
  "type": "snapshot",
  "ts": "2026-11-14T12:00:00.123Z",
  "payload": {
    "server": {"version": "0.2.0", "mode": "mock", "started_at": "...", "uptime_s": 1.2},
    "player": {"...": "PlayerState"},
    "queue": {"...": "QueueDTO"},
    "library": {"shelf": [{"...": "TrackDTO"}], "total": 9}
  }
}
```

### События P1/P2

| type | payload | когда |
|---|---|---|
| `player.state_changed` | `{state: PlayerState}` | любой переход статуса, seek, volume |
| `player.track_changed` | `{state: PlayerState}` | смена текущего трека (в т.ч. null) |
| `player.position` | `{position_ms, duration_ms, status, server_time_ms}` | ≤2 Гц, только `visible`-клиентам |
| `queue.updated` | `{queue: QueueDTO}` | добавление/удаление/reorder/авто-next |
| `volume.changed` | `{volume: int}` | изменение громкости |
| `system.warning` | `{code, message}` | например `mpv_restarted`, `audio_error` |
| `library.import.started` | `{job: ImportJob}` | импорт начал сканирование |
| `library.import.progress` | `{job: ImportJob}` | прогресс импорта/проверки |
| `library.import.completed` | `{job: ImportJob}` | импорт завершён |
| `library.import.failed` | `{job: ImportJob, error}` | фатальная ошибка job |
| `library.import.cancelled` | `{job: ImportJob}` | отмена |
| `library.updated` | `{tracks, missing}` | после импорта; клиенты перечитывают полки |
| `auth.required` | `{reason}` | токен истёк/невалиден; UI показывает Setup/login |
| `auth.status` | `{authenticated, login, expires_at, source}` | login/logout/refresh |
| `yandex.catalog_updated` | `{kind, count}` | likes/playlists/album/artist обновлены |
| `wave.updated` | `{wave: WaveState}` | старт волны/feedback/refill |
| `download.queued` | `{task: DownloadTask}` | задача добавлена/возобновлена |
| `download.progress` | `{task: DownloadTask}` | прогресс (не чаще 2 Гц) |
| `download.done` | `{task: DownloadTask}` | файл проверен, offline |
| `download.failed` | `{task: DownloadTask}` | ошибка/исчерпаны попытки |
| `lyrics.updated` | `{track_id, available}` | текст найден/закэширован |
| `pong` | `{}` | ответ на `ping` |

Position-события не вытесняют критичные: при переполнении очереди подписчика
удаляется старейшая позиция, `player.track_changed`/`library.*` сохраняются
(ADR-013). После переполнения/реконнекта снапшот восстанавливает состояние.

События будущих фаз (`lyrics.updated`, `download.progress`, `sync.*`,
`bt.device_*`, `network.status`) в P3 не отправляются.

### Сообщения клиента

```json
{"type": "client.hello", "payload": {"visible": true}}
{"type": "ping"}
```

- `client.hello` обязателен для получения `player.position`; UI отправляет при
  подключении и на `visibilitychange`;
- неизвестный тип → `{"type":"error","payload":{"code":"bad_message","message":"..."}}`
  (соединение не закрывается).

Позиция на клиенте интерполируется по `server_time_ms` (ADR-007).

## 8. Downloads — P4

Все mutation-маршруты идут через `require_mutation_access`; сейчас это no-op, LAN открыт (PIN removed, ADR-EX-001).
Скачивание идёт только с backend; токены не покидают сервер.

### 9.1 `DownloadTask`

```json
{
  "id": 12,
  "track_id": 501,
  "title": "Track",
  "status": "queued|downloading|paused|completed|failed|cancelled",
  "priority": 0,
  "attempts": 0,
  "bytes_done": 0,
  "bytes_total": 12345678,
  "target_path": "Artist/Album (2024)/Disc 1/01 - Track.flac",
  "quality": "flac",
  "format": "m4a",
  "error": null,
  "created_at": "...",
  "updated_at": "..."
}
```

`format` — фактический контейнер/кодек (`flac`, `m4a`, `mp3`, …), не обещанный.
`quality` может быть `null`, если схема задачи не хранит обещанное качество;
UI показывает фактический `format`. Несколько качеств одного трека — несколько
`media_files`; UI показывает фактический формат.

### 9.2 Маршруты

#### `GET /api/v1/downloads?status=…`
`{"items":[DownloadTask],"total":N}` — очередь + история; newest first.

#### `POST /api/v1/downloads`
Body: `{"track_id": 501}` **или** `{"album_id": 17}` **или**
`{"playlist_id": "uid:kind"}`, опционально `priority`. Возвращает `202`:
`{"tasks":[DownloadTask]}` (для альбома/плейлиста — все треки).
Дубликат уже `completed`+verified не ставится в очередь (skip).

#### `DELETE /api/v1/downloads/{id}`
Отменяет активную задачу и удаляет запись очереди; `204`. Temp-файл чистится.

#### `POST /api/v1/downloads/{id}/cancel`
Мягкая отмена: `status=cancelled`, partial сохраняется для resume; `200 DownloadTask`.

#### `POST /api/v1/downloads/{id}/retry`
Сбрасывает attempts/error и ставит в очередь; `200 DownloadTask`.

### 9.3 Pipeline (контракт)

1. resolve internal track → external id, metadata (P3 repository);
2. `download_info/file_info` → quality order `flac → aac → mp3` (без
   транскодирования), фактический codec/bitrate;
3. direct URL или `encraw` (`FileDownloadInfo.key` + AES-128-CTR);
4. stream chunks → temp файл `.part` в downloads root, fsync;
5. tags (mutagen), embedded cover 1000px + `cover.jpg`;
6. atomic rename по `downloads.paths` шаблону, traversal guard;
7. SHA-256 verify → `media_files.verified_at`, `has_local_file=1` (offline);
8. `library.updated` + `download.done`; ошибка → retry/backoff или failed.
Anti-ban: **1 параллельная**, пауза 2–5 с random, retry 429/5xx с backoff.
Возобновление — Range, если upstream поддерживает, иначе с нуля.
Очередь персистится в `download_tasks` (status/attempts/bytes/error) и
восстанавливается после рестарта.

### 9.4 `DownloadManager` interface

```python
class DownloadManager(Protocol):
    async def start_track(self, track_id: int, *, priority: int = 0) -> DownloadTask: ...
    async def start_album(self, album_id: int, *, priority: int = 0) -> list[DownloadTask]: ...
    async def start_playlist(self, playlist_id: str, *, priority: int = 0) -> list[DownloadTask]: ...
    async def list(self, *, status: str | None = None) -> DownloadTaskPage: ...
    async def cancel(self, task_id: int, *, remove: bool = False) -> DownloadTask | None: ...
    async def retry(self, task_id: int) -> DownloadTask: ...
    async def shutdown(self) -> None: ...
```

### 9.5 Ограничения

- Real-download на Pi разрешён владельцем (power risk accepted); backup БД —
  перед первой записью. Real smoke выполнен: один `flac-mp4` трек, verify/offline,
  local `/stream` 206; полный album/playlist — pending.
- Без транскодирования; FLAC в MP4 (`flac-mp4`) сохраняется как `.m4a`,
  mutagen-теги для такого контейнера могут быть недоступны — фиксируется как
  ограничение, не подмена формата.

## 9. Lyrics — P5

### `GET /api/v1/lyrics/{track_id}?language=ru`

Открытый read-only. Сначала локальный `.lrc`/`lyrics` cache, затем Яндекс
`tracks_lyrics` (LRC, иначе TEXT), затем LRCLIB (community fallback, только
artist/title/album/duration); ответ кэшируется в SQLite.

```json
{
  "track_id": 6,
  "available": true,
  "format": "lrc",
  "language": "ru",
  "source": "yandex|lrclib|lrc|plain",
  "offset_ms": 0,
  "lines": [{"time_ms": 1200, "text": "..."}],
  "plain": null
}
```

Нет текста: `200 {"available":false,"format":"none","lines":[],"plain":null}`.
`offset_ms` — сохранённое смещение трека; UI применяет его при подсветке.
WS `lyrics.updated` `{track_id, available}` после появления кэша.

### `PUT /api/v1/lyrics/{track_id}/offset` — mutation (PIN removed; LAN open)

`{"offset_ms": -500}` → `LyricsDTO`; применяется ко всем зонам (per-zone — P7).

## 9.5 History — P1

### `GET /api/v1/history?limit=50&offset=0`
```json
{"items":[{"id":1,"track":{"...":"TrackDTO"},"played_at":"...","zone_id":"local"}],
 "total":1,"limit":50,"offset":0}
```
Newest first; read-only. `play_history` пишется PlayerManager при старте трека.

### `DELETE /api/v1/history` — mutation (PIN removed; LAN open)
`{"deleted": N}` — очищает историю, треки/файлы не трогает.

## 10. Sync — P6

### `GET /api/v1/sync/status`
```json
{"running":false,"last":{...SyncRun},"next_run_at":"...","interval_s":3600,"progress":null}
```

### `GET /api/v1/sync/history?limit=20`
`{"items":[SyncRun],"total":N}` — newest first.

### `POST /api/v1/sync/start` — mutation (PIN removed; LAN open)
`202 SyncStatus`; повторный при running → `409 sync_running`.

### `POST /api/v1/sync/cancel` — mutation (PIN removed; LAN open)
Отмена между плейлистами; `202 SyncRun`/`409 sync_not_running`.

WS: `sync.started`, `sync.progress` `{phase,current,total}`, `sync.completed`,
`sync.failed`, `sync.cancelled`; payload run без секретов.

**Алгоритм:** likes/playlists из Yandex (или mock) → diff по `revision`,
fallback по составу; upsert в `playlists`/`playlist_tracks`/`tracks`;
идемпотентно; один прогон (single-flight); 429/5xx backoff; ошибка одного
плейлиста → `partial`; `sync_runs` хранит counts/error. Hourly при
`sync.enabled`, иначе только manual. On startup — не запускать автоматически
без `enabled`.

## 11. Zones / Bluetooth — P7

Zones: `GET /api/v1/zones` (open) → `{"items":[AudioZone],"total":N}`;
`POST /zones/{id}/activate`, `POST /zones/{id}/volume {volume}`, 
`POST /zones/{id}/offset {offset_ms}` — mutation (PIN removed; LAN open). Смена sink не прерывает
трек (перемещается sink-input mpv); local/browser/bt.

Bluetooth: `GET /bluetooth/devices`; `POST /bluetooth/scan`,
`POST /bluetooth/pair {address}`, `POST /bluetooth/connect {address}`,
`POST /bluetooth/forget {address}` — mutation (PIN removed; LAN open). A2DP sink регистрируется
как zone; auto-reconnect при появлении.

Real PipeWire discovery: `pactl list short sinks`; activation — default sink +
move sink-inputs. BT — `bluetoothctl`/BlueZ; mock без железа.
WS: `zones.updated`, `bt.device_*`.

## 12. Reliability: backups, settings, maintenance — P8

### `GET /api/v1/system/backups` (open)
`{"items":[{"name","size_bytes","created_at","verified"}],"total","directory"}`.
Только файлы `musicnx-*.db` из `paths.backups`; имена не раскрывают пути.

### `POST /api/v1/system/backups` — mutation (PIN removed; LAN open)
SQLite Backup API → `musicnx-YYYYmmdd-HHMMSS.db`; `201 BackupInfo`; вызывается
также перед real-import/download (P8 hardening).

### `GET /api/v1/system/integrity` (open)
`{"ok","database","quick_check","schema_version"}` — `PRAGMA quick_check`.

### `POST /api/v1/system/maintenance` — mutation (PIN removed; LAN open)
Удаляет старые `.part`/временные файлы, WAL checkpoint, quick_check;
`MaintenanceResult`; maintenance timer запускается при заданном
`maintenance.interval_s` в таблице `settings` (по умолчанию выключен).

### `GET /settings` (open)
`{"values":{"player.default_volume":70,"downloads.quality":"auto","sync.enabled":false},"editable":[...]}`.

### `PUT /settings` — mutation (PIN removed; LAN open)
Partial whitelist: `player.default_volume` (0..100, immediately applied),
`downloads.quality` (`auto|flac|aac|mp3`, applied to the next download resolve;
`auto` and explicit choices fall back honestly, the task badge shows the actual
container). `sync.enabled` отдаётся в `values`, но пока **read-only**: PUT с ним
→ 422 (меняется в `config.yaml`, нужен restart; live-toggle появится вместе с
`SyncService.set_enabled`). Неизвестный ключ → 422; значение сохраняется в
`settings` и переживает restart. Секреты/PIN не отдаются.

**Restore:** только CLI `scripts/restore.sh` (остановить сервис, проверить
backup, заменить БД); live API restore не делается — это честная граница.

## 13. Network / Wi-Fi — P8

`GET /api/v1/network/status` (open) → `NetworkStatus`:
`backend` (`mock|nmcli`), connected/interface/ssid/ip/signal/internet/message.

`POST /api/v1/network/scan` (mutation; PIN removed) → `WifiScanPage`
(`ssid`, `signal`, `security`, `connected`); без паролей.

`POST /api/v1/network/connect` (mutation; PIN removed)
`{"ssid":"...","password":"..."}` → `NetworkStatus`; пароль не логируется,
не сохраняется в БД и не возвращается. `nmcli` вызывается argv-списком, без shell.

`POST /api/v1/network/disconnect` (mutation; PIN removed) → `NetworkStatus`.

AP-onboarding и смена PIN/TLS — **не реализованы** в P8; в UI показываются как
граница, не кнопками. Failover — штатный NetworkManager (Ethernet > Wi-Fi);
сервис только читает фактическое состояние.

## 14. Нереализованные разделы SPEC §8 (появятся в своих фазах)

Маршрутов нет; запрос вернёт 404. Мини-PIN и локальная библиотека уже
реализованы (разделы 4–5 выше).

| Раздел | Маршруты | Фаза |
|---|---|---|
| settings | `GET /settings`, `PUT /settings` | P8 (deps `require_mutation_access` уже готов) |

Причина отсутствия: правило «одна фаза за раз» (AGENTS.md §2) и запрет
фиктивных заглушек (§12). `GET /api/v1/settings` не возвращает пустой объект —
его нет; `/stream` и Yandex-маршруты реализованы в P3.

