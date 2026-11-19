# База данных MusicNX

SQLite (WAL) — единственное хранилище состояния. Схема создаётся только
миграциями. Источник истины по требованиям — [SPEC.md](../SPEC.md) §6, §11.

## 1. Pragmas и режим подключения

При каждом открытии соединения (`backend/app/database/connection.py`):

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

Проверяются тестом `test_migrations.py` (`journal_mode=wal`, `foreign_keys=1`).
Один коннект на процесс, запись через `asyncio.Lock` и явные транзакции.
Позиция плеера не пишется каждый тик (SPEC §11.2): debounce 7 с + события
pause/stop/seek/eof/track change/shutdown.

Путь по умолчанию: `var/musicnx.db` (на Pi — `/var/lib/musicnx/database/musicnx.db`).

## 2. Миграции

- Файлы `backend/app/database/migrations/NNNN_name.sql`, применяются по возрастанию.
- Учёт — таблица `schema_migrations(version, name, applied_at)`.
- Каждая миграция выполняется в транзакции: `executescript` + запись версии;
  ошибка откатывает всё и не даёт приложению стартовать.
- Применённые версии не переписываются. Изменение схемы — новый файл.
- Миграция `0001_init.sql` создаёт все таблицы §6; `0002_search.sql` — первая
  (contentless) версия FTS5; `0003_library_search.sql` пересоздаёт FTS5 как
  обычную таблицу, добавляет `tracks.label` и индекс `media_files.checksum`.
- Текущая версия схемы — **4** (`schema_migrations`).
- Отдельной команды миграции нет: применяются при старте (быстро, идемпотентно).

## 3. Общие соглашения

- Время — TEXT ISO-8601 UTC (`2026-11-14T12:00:00.000Z`).
- Внешние идентификаторы — TEXT в namespace источника; для Яндекса трек —
  `trackId:albumId`. Уникальность `(source_id, external_id)`.
- Булевы признаки — INTEGER 0/1 с CHECK.
- `tracks` — одна композиция; файлы разного качества — строки `media_files`.
- Плейлисты ссылаются на треки, не копируют файлы.
- Удаление внешнего трека из плейлиста не удаляет локальный файл (tombstone
  `removed_at`), — SPEC §6.
- Ссылки на аудио не хранятся: только внешний ID и локальные пути файлов.

## 4. Таблицы

### 4.1 Справочники

`sources`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| name | TEXT | NOT NULL UNIQUE (`local`, `mock`, `yandex`) |
| created_at | TEXT | NOT NULL DEFAULT now |

`artists`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| name | TEXT | NOT NULL |
| sort_name | TEXT | NULL |
| cover_path | TEXT | NULL |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

Индексы: `idx_artists_name(name)`.

`artist_sources`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| artist_id | INTEGER | NOT NULL FK artists ON DELETE CASCADE |
| source_id | INTEGER | NOT NULL FK sources ON DELETE CASCADE |
| external_id | TEXT | NOT NULL |

Уникальность: `(source_id, external_id)`, `(artist_id, source_id)`.

`albums`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| title | TEXT | NOT NULL |
| artist_id | INTEGER | NULL FK artists ON DELETE SET NULL |
| year | INTEGER | NULL |
| cover_path | TEXT | NULL |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

Индексы: `idx_albums_artist(artist_id)`, `idx_albums_title(title)`.

`album_sources` — как `artist_sources`, но `album_id`; уникальность
`(source_id, external_id)`, `(album_id, source_id)`.

### 4.2 Треки и файлы

`tracks`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| title | TEXT | NOT NULL |
| album_id | INTEGER | NULL FK albums ON DELETE SET NULL |
| duration_ms | INTEGER | NULL, CHECK ≥ 0 |
| genre | TEXT | NULL |
| label | TEXT | NULL (тег label, добавлен миграцией 0003) |
| year | INTEGER | NULL |
| track_number | INTEGER | NULL |
| disc_number | INTEGER | NULL |
| has_local_file | INTEGER | NOT NULL DEFAULT 0 CHECK IN (0,1) |
| has_online_source | INTEGER | NOT NULL DEFAULT 0 CHECK IN (0,1) |
| file_missing | INTEGER | NOT NULL DEFAULT 0 CHECK IN (0,1) |
| source_unavailable | INTEGER | NOT NULL DEFAULT 0 CHECK IN (0,1) |
| last_verified_at | TEXT | NULL |
| deleted_at | TEXT | NULL; tombstone миграции 0004, трек скрыт из выдачи |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

Индексы: `idx_tracks_album(album_id)`, `idx_tracks_title(title)`,
`idx_tracks_availability(has_local_file, has_online_source)`,
`idx_tracks_deleted_at(deleted_at)`.

**Вычисляемый бейдж** (не хранится отдельным enum, SPEC §6):

| Приоритет | Условие | Бейдж |
|---|---|---|
| 1 | `file_missing=1` | `missing` |
| 2 | `has_local_file=1` | `offline` |
| 3 | `has_online_source=1 AND source_unavailable=1` | `unavailable` |
| 4 | `has_online_source=1` | `online` |
| 5 | иначе | `metadata_only` |

Трек может быть одновременно offline и online — хранятся оба признака, бейдж
показывает приоритетное состояние (UI может показать оба). Реализация:
`backend/app/core/availability.py`, тест `test_availability.py`.

`track_sources`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| source_id | INTEGER | NOT NULL FK sources ON DELETE CASCADE |
| external_id | TEXT | NOT NULL |
| availability | TEXT | NOT NULL DEFAULT `unknown`, CHECK IN (`available`,`unavailable`,`unknown`,`region_restricted`) |
| last_checked_at | TEXT | NULL |

Уникальность: `(source_id, external_id)`, `(track_id, source_id)`.
Индекс: `idx_track_sources_track(track_id)`.

`track_artists`

| Колонка | Тип | Ограничения |
|---|---|---|
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| artist_id | INTEGER | NOT NULL FK artists ON DELETE CASCADE |
| position | INTEGER | NOT NULL DEFAULT 0 |

PK `(track_id, artist_id)`; индекс `idx_track_artists_artist(artist_id)`.

`media_files`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| role | TEXT | NOT NULL DEFAULT `library`, CHECK IN (`library`,`download`,`cache`) |
| path | TEXT | NOT NULL UNIQUE |
| format | TEXT | NULL (`mp3`,`flac`,`aac`,`m4a`,`wav`,…) |
| codec | TEXT | NULL |
| bitrate | INTEGER | NULL |
| sample_rate | INTEGER | NULL |
| channels | INTEGER | NULL |
| size_bytes | INTEGER | NULL |
| duration_ms | INTEGER | NULL |
| checksum | TEXT | NULL |
| verified_at | TEXT | NULL |
| created_at | TEXT | NOT NULL DEFAULT now |

Индекс: `idx_media_files_track(track_id)` и `idx_media_files_checksum(checksum)`.

### 4.3 Плейлисты

`playlists`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| title | TEXT | NOT NULL |
| source_id | INTEGER | NULL FK sources ON DELETE SET NULL |
| external_id | TEXT | NULL |
| revision | TEXT | NULL |
| kind | TEXT | NOT NULL DEFAULT `user`, CHECK IN (`user`,`likes`,`wave`,`local`) |
| description | TEXT | NULL |
| cover_path | TEXT | NULL |
| track_count | INTEGER | NOT NULL DEFAULT 0 |
| synced_at | TEXT | NULL |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

Уникальность: `(source_id, external_id)` (локальные плейлисты с NULL допускаются
несколько). Индекс: `idx_playlists_kind(kind)`.

`playlist_tracks`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| playlist_id | INTEGER | NOT NULL FK playlists ON DELETE CASCADE |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| position | INTEGER | NOT NULL CHECK ≥ 0 |
| added_at | TEXT | NOT NULL DEFAULT now |
| removed_at | TEXT | NULL (tombstone при удалении из внешнего плейлиста) |

Уникальность: `(playlist_id, position)`. **Трек может повторяться в плейлисте** —
уникальности по `track_id` нет. Позиции удалённых (tombstone) строк сохраняются,
активные строки читаются `WHERE removed_at IS NULL ORDER BY position`.

### 4.4 Тексты

`lyrics`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| language | TEXT | NOT NULL DEFAULT `und` |
| format | TEXT | NOT NULL CHECK IN (`plain`,`lrc`) |
| content | TEXT | NOT NULL |
| source | TEXT | NULL (`yandex`,`file`,`user`) |
| offset_ms | INTEGER | NOT NULL DEFAULT 0 |
| updated_at | TEXT | NOT NULL DEFAULT now |

Уникальность: `(track_id, language, format)`; индекс `idx_lyrics_track(track_id)`.

### 4.5 История и состояние

`play_history`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| played_at | TEXT | NOT NULL DEFAULT now |
| zone_id | TEXT | NULL |

Индексы: `idx_play_history_played(played_at DESC)`, `idx_play_history_track(track_id)`.
Пишется при старте трека (P1), не при каждом тике.

`playback_state` — singleton

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK CHECK (id = 1) |
| track_id | INTEGER | NULL FK tracks ON DELETE SET NULL |
| queue_item_id | INTEGER | NULL FK queue_items ON DELETE SET NULL |
| position_ms | INTEGER | NOT NULL DEFAULT 0 CHECK ≥ 0 |
| volume | INTEGER | NOT NULL DEFAULT 70 CHECK 0..100 |
| status | TEXT | NOT NULL DEFAULT `stopped`, CHECK IN (`stopped`,`playing`,`paused`,`buffering`,`error`) |
| zone_id | TEXT | NULL |
| queue_index | INTEGER | NOT NULL DEFAULT 0 CHECK ≥ 0 |
| updated_at | TEXT | NOT NULL DEFAULT now |

`queue_item_id`/`queue_index` добавлены к полям SPEC §6, чтобы точно
восстанавливать текущий элемент (очередь допускает повторы трека).

### 4.6 Очередь

`queue_items`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE |
| position | INTEGER | NOT NULL UNIQUE CHECK ≥ 0 |
| source_playlist_id | INTEGER | NULL FK playlists ON DELETE SET NULL |
| added_at | TEXT | NOT NULL DEFAULT now |

Индексы: `idx_queue_items_track(track_id)`, `idx_queue_items_playlist(source_playlist_id)`.
Позиции перенумеровываются 0..n-1 в транзакции при добавлении/удалении/reorder.

### 4.7 Загрузки (таблица; пайплайн — P4)

`download_tasks`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| track_id | INTEGER | NOT NULL FK tracks ON DELETE CASCADE, UNIQUE |
| status | TEXT | NOT NULL DEFAULT `queued`, CHECK IN (`queued`,`downloading`,`paused`,`completed`,`failed`,`cancelled`) |
| priority | INTEGER | NOT NULL DEFAULT 0 |
| attempts | INTEGER | NOT NULL DEFAULT 0 |
| bytes_done | INTEGER | NOT NULL DEFAULT 0 |
| bytes_total | INTEGER | NULL |
| target_path | TEXT | NULL |
| error | TEXT | NULL |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

Индексы: `idx_download_tasks_status(status)`, `idx_download_tasks_priority(priority DESC)`.
В P1 таблица не используется; API загрузок отсутствует.

### 4.8 Зоны (таблица; реализация — P7)

`audio_zones`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | TEXT | PK |
| kind | TEXT | NOT NULL CHECK IN (`local_sink`,`browser`,`bt`) |
| sink_name | TEXT | NULL |
| volume | INTEGER | NOT NULL DEFAULT 100 CHECK 0..100 |
| delay_offset_ms | INTEGER | NOT NULL DEFAULT 0 |
| last_seen | TEXT | NULL |
| created_at, updated_at | TEXT | NOT NULL DEFAULT now |

### 4.9 Синхронизация (таблица; движок — P6)

`sync_runs`

| Колонка | Тип | Ограничения |
|---|---|---|
| id | INTEGER | PK AUTOINCREMENT |
| started_at | TEXT | NOT NULL |
| finished_at | TEXT | NULL |
| status | TEXT | NOT NULL CHECK IN (`running`,`success`,`partial`,`failed`,`cancelled`) |
| playlists_added / playlists_updated / playlists_removed | INTEGER | NOT NULL DEFAULT 0 |
| tracks_added / tracks_updated / tracks_removed | INTEGER | NOT NULL DEFAULT 0 |
| error | TEXT | NULL |

Индекс: `idx_sync_runs_started(started_at DESC)`.

### 4.10 Служебные

`settings`

| Колонка | Тип | Ограничения |
|---|---|---|
| key | TEXT | PK |
| value | TEXT | NOT NULL (JSON-значение) |
| updated_at | TEXT | NOT NULL DEFAULT now |

`schema_migrations`

| Колонка | Тип | Ограничения |
|---|---|---|
| version | INTEGER | PK |
| name | TEXT | NOT NULL |
| applied_at | TEXT | NOT NULL DEFAULT now |

## 5. Поиск

- FTS5 `tracks_fts(title, artists, album)` — обычная (не contentless) таблица,
  `rowid` = `tracks.id`. Создаётся/пересоздаётся миграцией
  `0003_library_search.sql`, чтобы строки можно было обновлять и удалять.
- Индекс заполняется импортом при создании/обновлении трека; при старте
  приложение сверяет `COUNT(tracks)` и `COUNT(tracks_fts)` и при расхождении
  перестраивает индекс (`LibraryRepository.rebuild_search_index_if_needed`).
- Запрос: `tracks_fts MATCH ?`, токены из пользовательского ввода очищаются
  (буквы/цифры) и дополняются `*` для prefix-поиска. Небезопасные символы FTS
  не попадают в запрос.
- Fallback: при пустом/недоступном FTS — `LIKE` по title/artist (используется
  тестами миграции и старыми БД до перестройки).
- Поиск по альбомам/исполнителям — `LIKE` по названию (отдельные эндпоинты).
- Сортировка списков — `title COLLATE NOCASE`/`name`, пагинация `limit/offset`.

## 5.1 Импорт локальной медиатеки (P2)

- Источник `local`; `external_id = local:<relative_path>` внутри корня импорта.
  Это даёт идемпотентность при повторном сканировании.
- Дедупликация:
  1. `(source_id, external_id)` — точное совпадение;
  2. иначе title + artists + album + duration. Длительность сравнивается с
     допуском **1500 мс** (контейнеры MP3/FLAC/AAC по-разному считают padding);
  3. только по названию не склеиваем; разные album/исполнители/длительность
     дают отдельный трек.
- Несколько файлов одного трека (MP3 + FLAC, разные качества) — строки
  `media_files` с разными `path`; `media_files.path` уникален.
- После вставки файла `has_local_file` остаётся 0, пока checksum/verify не
  завершится: `verified_at` в `media_files` + `tracks.has_local_file=1`.
  Только после этого UI показывает `offline` (SPEC §7.6).
- Пропавший файл: `file_missing=1`, `has_local_file=0`, запись трека/альбома
  сохраняется; `media_files` не удаляется.
- Обложки: `albums.cover_path` хранит ключ кэша (`<sha256[0:32]>`), файлы —
  `paths.cache/covers/<key>-{64,300,1000}.jpg` (64 — pre-blur). LRU-лимит
  `library.cover_cache_mb`.

## 6. Надёжность и обслуживание (SPEC §11)

1. WAL, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`.
2. Позиция — debounce 7 с + смена статуса/трека/seek/eof/shutdown.
3. Метаданные — пакетными транзакциями (P2+).
4. Ротация логов вне БД (`RotatingFileHandler`, 5 MB × 3).
5. Кэш обложек — LRU (P2).
6. Временные файлы — очистка в P4/P8.
7. Бэкап — SQLite Backup API на другой носитель (P8), не `cp` активного файла.
8. `PRAGMA quick_check` при обслуживании (P8).
9. Корректное `db.close()` при остановке (lifespan).
10. `noatime` для носителя — рекомендация в install (docs/DEPLOYMENT.md).
11. Библиотека не сканируется постоянно (P2: по событию/вручную).
12. Состояние восстанавливается после неожиданной перезагрузки (snapshot БД;
    playing → paused, ADR-004).

## 7. Seed mock-каталога

Только `mode=mock` и `mock.seed_catalog=true`. Идемпотентно: `INSERT OR IGNORE`
по `sources.name`, `track_sources(source_id, external_id)`, плейлистам.
Источник — `mock`; треки имеют `has_online_source=1`, `has_local_file=0`,
`media_files` отсутствуют — mock backend играет виртуальную длительность.
Повторный старт не создаёт дубликатов; это проверяет тест
`backend/tests/test_queue_manager.py::test_seed_is_idempotent`.
