# Yandex Music API — подтверждённые методы (gate)

**Пакет:** `yandex-music==3.2.2` (pinned).
**Источник:** интроспекция установленного пакета
(`inspect.signature(ClientAsync.*)`) + документация
[ym.marshal.dev](https://ym.marshal.dev/) и OpenAPI (`/api/`).
**Дата:** 2026-11-14.

Правило: **любой метод, которого нет в этом документе, запрещён** до отдельной
проверки и записи сюда. Не угадывать методы по названиям.

## 1. Клиент и авторизация

### `ClientAsync.__init__`

```python
ClientAsync(
    token: str | None = None,
    base_url: str | None = None,
    request: Request | None = None,
    language: str = "ru",
    report_unknown_fields: bool = False,
    json_backend: JsonBackend | None = None,
    strict: bool = False,
    on_schema_mismatch: Callable[[SchemaMismatch], None] | None = None,
)
```

Создаёт async-клиент. Сам по себе токен не проверяет; для проверки/логина
используется `init()`.

### `init`

```python
async ClientAsync.init(*args, **kwargs) -> Self
```

Загружает `self.me: Status` и `self.account_uid`; `me.account.login` — логин.
Используется для валидации manual token и получения login.

### Device Flow (неблокирующие части)

```python
async ClientAsync.request_device_code(
    device_id: str | None = None,
    device_name: str | None = None,
    client_id: str | None = None,
) -> DeviceCode

async ClientAsync.poll_device_token(
    device_code: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> OAuthToken | None
```

- `DeviceCode`: `device_code`, `user_code`, `verification_url`, `expires_in`,
  `interval`.
- `OAuthToken`: `access_token`, `refresh_token`, `expires_in`, `token_type`.
- `poll_device_token` возвращает `None`, пока пользователь не подтвердил
  (`authorization_pending`); остальные OAuth-ошибки бросают `DeviceAuthError`.
- **Не использовать** блокирующий `ClientAsync.device_auth(...)` в request
  handler: он ждёт подтверждения. Использовать `request_device_code` +
  фоновый poll.

### refresh

**В `yandex_music==3.2.2` метода refresh НЕТ** (проверено `dir(ClientAsync)`).
OAuth refresh выполняется прямым POST на:

```text
POST https://oauth.yandex.ru/token
grant_type=refresh_token
refresh_token=<refresh_token>
client_id=<yandex.client_id or встроенный client_id пакета>
client_secret=<yandex.client_secret or встроенный client_secret>
```

Это **OAuth-контракт**, не метод `yandex_music`; он применяется в
`integrations/yandex_music/auth.py`. Ответ: `access_token`, `refresh_token`,
`expires_in`. При ошибке refresh — `auth.required`, токен не отдавать наружу.

Device-flow client_id/secret по умолчанию — публичные креды Android-приложения
из `yandex_music._client_async.device_auth`; их можно переопределить в конфиге.

## 2. Каталог

Все методы ниже — `ClientAsync`, подтверждены интроспекцией.

| Метод | Сигнатура | Возвращает |
|---|---|---|
| likes | `users_likes_tracks(user_id=None, if_modified_since_revision=0, *args, **kwargs)` | `TracksList \| None` |
| playlists | `users_playlists_list(user_id=None, *args, **kwargs)` | `list[Playlist]` |
| playlist | `users_playlists(kind, user_id=None, *args, **kwargs)` | `Playlist \| list[Playlist] \| None` |
| playlist tracks | `Playlist.fetch_tracks_async(*args, **kwargs)` | `list[TrackShort]` |
| tracks by ids | `tracks(track_ids, with_positions=True, *args, **kwargs)` | `list[Track]` |
| album | `albums_with_tracks(album_id, *args, **kwargs)` | `Album \| None` |
| artist tracks | `artists_tracks(artist_id, page=0, page_size=20, *args, **kwargs)` | `ArtistTracks \| None` |
| search | `search(text, nocorrect=False, type_="all", page=0, playlist_in_best=True, *args, **kwargs)` | `Search \| None` |
| track | `tracks([track_id])` | `list[Track]` |

Детали:
- `users_playlists_list()` запрашивает **все** плейлисты пользователя;
  `users_playlists(kind=...)` — один/несколько по kind. Пустой `kind=[]`
  Yandex API отклоняет (`kinds: Parameter value is not set`), поэтому список
  берётся только через `users_playlists_list`.
- `TracksList.tracks: list[TrackShort]`; `TrackShort.track` может быть `None`,
  `TrackShort.id` — ID трека. Полные треки — `tracks([ids])`.
- `Playlist` содержит `uid`, `kind`, `title`, `track_count`, `revision`,
  `cover`/`og_image`; полный состав — `Playlist.fetch_tracks_async()`.
- `Album.volumes: list[list[Track]]`; `albums_with_tracks` заполняет volumes.
- `ArtistTracks.tracks: list[Track]`, `pager`.
- `Search.tracks/albums/artists/playlists` — `SearchResult[T]` с полем
  `results: list[T]`.
- `Track` поля (нужные): `id`, `title`, `artists`, `albums`, `duration_ms`,
  `available`, `available_for_premium_users`, `lyrics_available`,
  `cover_uri`, `preview_duration_ms`, `explicit`, `filename`, `error`.
- `Album`/`Artist`: `id`, `title`/`name`, `cover_uri`/`cover`, `year`,
  `track_count`, `artists`.

## 3. Стриминг

| Метод | Сигнатура | Возвращает |
|---|---|---|
| download info | `tracks_download_info(track_id, get_direct_links=False, *args, **kwargs)` | `list[DownloadInfo]` |
| direct link | `DownloadInfo.get_direct_link_async(timeout=..., **kwargs)` | `str` |
| file info | `tracks_file_info(track_id, quality="lossless", codecs=None, transport="raw", **kwargs)` | `FileInfo \| None` |
| decrypt | `yandex_music.utils.decrypt.decrypt_track(data: bytes, key: str) -> bytes` | bytes |

- `DownloadInfo`: `codec` (`mp3`,`aac`,…), `bitrate_in_kbps`, `gain`,
  `preview`, `direct` (**bool**), `direct_link` (подписанная ссылка при
  `get_direct_links=True`), `download_info_url`. Нельзя читать `direct` как URL.
- **Real P4 smoke 14.11.2026:** P4 source/pipeline скачал трек `flac-mp4` →
  29,745,710 bytes `.m4a`, `format=m4a`, verify/offline, local `/stream` 206.
- `FileInfo.download_info` — один `FileDownloadInfo` с полями `track_id`,
  `quality` (`lossless/hq/nq`), `codec` (`flac`, `flac-mp4`, `aac-mp4`,
  `he-aac-mp4`, `mp3`, …), `bitrate`, `transport` (`raw`/`encraw`), `url`,
  `urls`, `key`.
- `transport="encraw"` → файл зашифрован AES-128-CTR с нулевым counter;
  `FileDownloadInfo.download_async/download_bytes_async` расшифровывают
  автоматически. `decrypt_track` — подтверждённый helper; для потоковой
  загрузки P4 использовать `cryptography` AES-CTR по чанкам (тот же режим).
- `tracks_file_info` требует подписку для `lossless`; сервер выбирает лучший
  доступный codec. `flac-mp4`/`aac-mp4` — MP4-контейнер, bitrate может быть 0.
- **Real smoke 14.11.2026:** `tracks_file_info` для трека вернул `flac-mp4`
  (29,529,693 bytes), `raw` и `encraw` (key present) дали одинаковый SHA-256;
  файл — `ftyp isom` MP4, mutagen не распознал FLAC-in-MP4. Это ограничение
  контейнера/кодека, не транскодирование; P4 фиксирует фактический формат.

## 4. Моя волна (rotor)

| Метод | Сигнатура | Возвращает |
|---|---|---|
| stations | `rotor_stations_list(language=None, *args, **kwargs)` | `list[StationResult]` |
| station tracks | `rotor_station_tracks(station, settings2=True, queue=None, *args, **kwargs)` | `StationTracksResult \| None` |
| station feedback | `rotor_station_feedback(station, type_, timestamp=None, from_=None, batch_id=None, total_played_seconds=None, track_id=None, stream_id=None, **kwargs)` | `bool` |

- `StationTracksResult.sequence: list[Sequence]`; `Sequence.track: Track`,
  `Sequence.type`, `Sequence.liked`; `batch_id`, `radio_session_id`.
- Подтверждённые типы feedback: **`radioStarted`, `trackStarted`,
  `trackFinished`, `skip`**. Для `skip` передавать `track_id` и
  `total_played_seconds`.
- Станция «Моя волна»: `user:onyourwave`; альтернативные seeds — по
  `StationResult.id_for_from` (не хардкодить чужие).
- **like/dislike в wave** реализуются подтверждёнными методами аккаунта:
  `users_likes_tracks_add/remove`, `users_dislikes_tracks_add/remove`
  (`track_ids` список/строка/число → `bool`). Это не `rotor_station_feedback`
  (его документированные типы не включают like/dislike); UI-семантика
  «нравится/не нравится» честно меняет аккаунт-лайки/дизлайки.
- Refill: повторный `rotor_station_tracks(station, queue=<last_track_id>)`.

## 5. Текст (P5, не использовать в P3)

`tracks_lyrics(track_id, format_="TEXT", **kwargs) -> TrackLyrics | None`.
Метод подтверждён, но P3 lyrics не реализует.

## 6. Нормализованная форма для mock/API

Адаптер возвращает внутренние DTO (`Track` с `source="yandex"`,
`external_id="<track_id>:<album_id>"`, `preview`), плейлисты/альбомы/артисты.
Mock-фикстуры обязаны повторять **эту нормализованную форму**, а не сырые
ответы библиотеки; поля: `id` (внутренний после upsert), `title`, `artists`,
`album`, `duration_ms`, `cover_url`, `availability`, `preview`, `liked`.

## 7. Запрещено

- Вызывать любой метод вне этого документа (в т.ч. «по аналогии»).
- Использовать `device_auth`/`poll_device_token` в request handler без
  фонового task.
- Хранить токены в `tracks`/`settings`/WS/логах.
- Использовать `DownloadInfo.download*` для отдачи браузеру через Python:
  `/stream` только проксирует HTTP Range, без буферизации файла в RAM.
