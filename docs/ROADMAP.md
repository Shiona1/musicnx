# Roadmap

Источник — [SPEC.md](../SPEC.md) §13; детальные чекбоксы — [PLAN.md](../PLAN.md).
Одна фаза за раз; каждая заканчивается рабочим состоянием, тестами и отчётом.

## Обзор

| Фаза | Содержание | Статус |
|---|---|---|
| P0 | Bootstrap: репо, docs, каркас, mock, тесты | **завершено** |
| P1 | Core + минимальный Web UI: mpv IPC, очередь, state machine, WS | **завершено** |
| P1.2 | systemd, reboot, mini-PIN, SPEC 32-bit/hostname, gate P4 | **завершено** |
| P2 | Media Library: импорт, теги, обложки, поиск, доступность | **завершено (mock)**; real-import pending БП |
| P3 | Яндекс: auth, каталог, стриминг, Волна, `/stream` | **завершено (mock)**; real-smoke pending токена |
| P4 | Downloads + офлайн | **завершено (mock + 1 real download)**; soak pending |
| P5 | Lyrics: parser, кэш, караоке | **завершено (mock)**; real lyrics pending |
| P6 | Sync Engine (hourly, diff, идемпотентность) | **завершено (mock)**; real hourly pending |
| P7 | Локальные зоны (jack/USB-DAC/BT) | **завершено (mock)**; real hardware pending |
| P8 | Надёжность + сеть + PIN-полишинг | не начато |
| P9+ | Snapcast, Ynison, DLNA, Chromecast, AirPlay, GPIO, OTA | только по команде |

## P0 — Bootstrap (завершено)

**Результат:** репозиторий с полным набором документов (§17 SPEC), FastAPI
каркас, SQLite + миграции, event bus, mock-адаптеры, Svelte-каркас, тесты и
dev-команды. Mock-режим работает без сети, токена и Pi.

**Критерий выхода:** `make lint && make test` зелёные; `make mock` отдаёт
`/system/health`, `/system/status`, `/library/tracks`; docs без заглушек.

## P1 — Core + минимальный Web UI (завершено)

**Результат:** mpv IPC-контроллер с автоперезапуском, очередь и state machine,
сохранение/восстановление, WS-шина (снапшот/события/позиция 2 Гц), REST
player/queue/system, минимальный UI с полкой, управлением и очередью,
синхронизация двух вкладок. Mock e2e; real — локальный трек.

**Критерий выхода:** две вкладки синхронны; play/pause/seek/next работают;
состояние переживает рестарт; тесты §13 P1 зелёные.

## P1.2 — Закрепление (завершено)

**Результат:** постоянный systemd user service с `%t`-runtime, reboot-проверка,
`gpu_mem=16`, mini-PIN для импорта/настроек, параметризованные hostname/user,
32-bit armv7l допущен, gate `yandex-music` на armv7l пройден. См. PLAN.md.

## P2 — Media Library (завершено в mock)

Импорт MP3/FLAC/AAC/M4A/WAV; сканирование по запросу/событию; теги mutagen;
обложки (300/1000/64) с LRU; дедупликация (external ID → title+artists+album+
duration с допуском 1.5 с); checksum в фоне; поиск FTS5; фильтры доступности;
API library tracks/albums/artists/search/import/artwork; UI Library с импортом,
прогрессом и PIN-диалогом.

**Критерий:** локальный файл импортируется, находится, играет, обложка
отображается; повторный импорт не создаёт дубликатов. Выполнено на mock
fixtures; **real-import на Pi — после замены БП** (docs/HARDWARE.md §9).

## P3 — Yandex (завершено в mock)

Device Flow + QR + ручной токен; refresh; каталог (плейлисты, лайки, альбомы,
исполнители, поиск); стриминг с выбором качества; обновление ссылок; browser
zone `/stream` + Range; «Моя волна» + feedback; UI Setup/Home/Now Playing.
Методы заморожены в [YANDEX_API.md](YANDEX_API.md), пакет 3.2.2.

**Критерий:** онлайн-трек играет при подписке; `/stream` перематывается; волна
реагирует на feedback; токены не видны фронтенду. Mock e2e — зелёный;
real-smoke — pending токена владельца (не в CI).

## P4 — Downloads (завершено mock + 1 real download)

Очередь 1 параллельно, антибан-пауза, ретраи/backoff, докачка, restart-safe;
FLAC→AAC→MP3 с честным бейджем; encraw; раскладка путей; теги; обложки;
проверка целостности → offline; UI Downloads. Реальный трек скачан на Pi,
`/stream` 206; полный альбом/плейлист и soak — pending.

**Критерий:** альбом скачивается, файл проверен, offline-статус честный,
повторная загрузка не дублирует, очередь переживает рестарт.

## P5 — Lyrics

Свой LRC parser + тесты; plain fallback; кэш SQLite + `.lrc`; подсветка строки
по позиции mpv + offset зоны; UI караоке.

**Критерий:** LRC отображается и синхронен; offset зоны применяется; без
таймкодов — обычный текст.

## P6 — Sync Engine

Hourly 3600 с / manual / on startup; diff по ревизиям с fallback на состав;
идемпотентность; `sync_runs`; partial success; 429 backoff; single-flight;
`last_attempt`/`last_success`; UI Sync Center. Алгоритм — docs/SYNC.md.

**Критерий:** повторная синхронизация без дубликатов; удаление из внешнего
плейлиста не удаляет файл; ошибка сети не портит библиотеку.

## P7 — Локальные зоны

PipeWire router; jack/USB-DAC/BT; переключение sink-input без прерывания;
per-zone volume/offset; BT pair/reconnect/forget; UI Zones/Quick Panel;
подсказка Ethernet при BT+Wi-Fi.

**Критерий:** переключение зоны на лету; громкость и offset запоминаются;
BT авто-реконнект.

## P8 — Надёжность + сеть

Бэкапы (Backup API), восстановление, integrity check; очистка кэша; maintenance
timer; длительные тесты RAM/CPU/temp; Wi-Fi страница; AP-onboarding; failover;
PIN-полишинг (хэш/смена/rate limit/CSRF/TLS; mini-PIN уже в P1.2, ADR-009);
install/update/backup скрипты, systemd units.

**Критерий:** 24-часовой прогон без утечек; бэкап восстанавливается; полный
PIN-полишинг защищает настройки; install.sh идемпотентен.

## P9+ — по отдельной команде

Snapcast/multiroom, Ynison, DLNA/UPnP, Chromecast, AirPlay, голосовое
управление, визуализаторы, нативные приложения, многопользовательскость,
тяжёлая обработка аудио, OTA. Архитектура оставляет границы, код не пишется.

## Отложенные решения

- Smart-синхронизация (адаптивный интервал).
- Общий дисковый кэш для browser zone (сейчас допустим двойной трафик).
- SBC-XQ для BT.
- USB Wi-Fi 5 ГГц как апгрейд.
- Автопродолжение при старте (сейчас restore → paused, ADR-004).

## UI-полировка по двухосевой критике (2026-11-19, завершено)

Владелец задал планку «Яндекс Музыка / Spotify». Двухагентная критика
(`impeccable critique`, снапшоты в `.impeccable/critique/`)
дали базовую оценку **28/40** и 5 приоритетных проблем; шесть шагов
полировки закрыли их все:

| Шаг | Коммит | Содержание |
|---|---|---|
| harden | `6a1e028` | модалка тегов на общем Modal-паттерне (`lib/modal.ts`), WAI-ARIA tablist со стрелками, двухшаговые подтверждения, видимый фокус seek/поиска |
| clarify | `bd63a6f` | 6 битых русских литералов + регресс-тест `test_ui_literals.py`, бытовой язык «Настроек» |
| polish | `7a4ed6b` | двухтоновые тосты (`ToastTone`), снят глобальный busy-лок, градиентные primary-кнопки уплощены по контракту DESIGN_SYSTEM |
| adapt | `2613b61` | `BottomNav` в thumb-zone ≤900px, seek в мобильном доке, таргеты ≥44px при `(pointer: coarse)`, `hover:none`-fallback, touch-action/overscroll |
| shape | `bb60603`, `8833993` | shuffle/repeat (полный стек, персист), менеджмент очереди («Далее», reorder, очистка), instant-поиск (локальный 350 мс / Яндекс 600 мс), детальные страницы альбомов/исполнителей с URL-роутингом |
| финал | `1896f86` | русская плюрализация («1 трек / 2 трека / 5 треков») через общие хелперы `lib/art.ts` |

Повторная критика: **34/40** (тренд 28 → 34), живой smoke подтвердил
режимы, очередь, instant-поиск, deep-link детальных страниц и ESC в
модалке тегов. Детектор `impeccable detect` — чист (2 advisory —
подтверждённые false positive на имени `@keyframes bounce`).

**Осталось (бэклог следующей итерации, по приоритету):**
- [P1] единый ConfirmDialog/PromptDialog вместо `window.confirm/prompt`
  (HistoryShelf, LibraryView.removeTrack, CatalogView rename/delete,
  Home.createPlaylist) + фокус-трап в остальных 7 PIN-модалках через
  готовый `lib/modal.ts`.
- [P1] дизамбигуатор неотличимых дублей в строках треков (файл/источник).
- [P2] PWA: `viewport-fit=cover` + Media Session API (гарнитура/локскрин).
- [P2] степперы «Зон» ≥44px при `(pointer: coarse)` для ширины >480px.
- [P3] единый live-паттерн поиска (Главная Яндекса — кнопкой),
  `history.replaceState` для живого запроса, скелетоны в Медиатеке.
- Отдельный код-трек: дубликат «Городские огни» в полке (7→8 при 6
  уникальных) — симптом дедупликации/синка, не UI.
