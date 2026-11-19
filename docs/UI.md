# Web UI MusicNX

Визуальный ориентир — CardsNX/HomeNX (SPEC §9): тёмная тема, крупные карточки
обложек на матовом стекле, горизонтальные полки, большой Now Playing с
размытым фоном, выдвижная быстрая панель. Эффекты — на клиенте; сервер отдаёт
статику и обложки. Анимации — только `transform`/`opacity`.

## 0. App Shell (Stage 1, реализовано)

- **SystemBar:** 5 основных разделов (`Сейчас / Медиатека / Яндекс / Загрузки /
  Система`); на desktop вторичная строка `Синхронизация / Зоны / Сеть / Настройки`,
  на mobile — компактное меню (primary + system + статус сервера).
- **URL:** `/`, `/library`, `/yandex`, `/yandex/setup`, `/downloads`,
  `/system/{sync,zones,network,settings}`; `pushState` + `popstate`, refresh
  сохраняет раздел. `/setup` остаётся AP-порталом; FastAPI SPA fallback отдаёт
  shell для SPA-путей и 404 для неизвестных `/api/*`.
- **PageShell:** единая ширина `--page-max: 1680px`, gutters, dock reserve.
  Страницы Stage 1 ещё используют свои внутренние карточки (их заменит Stage 2+).
- **NowDock:** постоянный нижний плеер (обложка, трек, prev/play/next, seek,
  volume, zone, queue count, quick menu: zones/network/sleep). Один `NowHero` для
  local/Яндекс; старые `NowPlaying`/`YandexNowPlaying` удалены.
- **QueueDrawer / QueueSheet:** очередь из NowDock; desktop — правый drawer,
  mobile — нижний sheet.
- **Токены:** `frontend/src/styles/tokens.css`; Inter 500/600/700 локально
  (`frontend/src/assets/fonts/`, OFL); frosted glass, cyan/violet fallback,
  reduced-motion/transparency, `?lite=1`.
- **LG2:** SystemBar и NowDock используют `material-chrome` (20px desktop / 16px
  mobile), mobile menu и quick menu — `material-overlay`; ambient blur снижен до
  34px; акцент обложки извлекается на клиенте (32×32 canvas, кэш, fallback
  cyan/violet). Тела страниц ещё не переработаны; legacy content blur остаётся
  до LG4. Отчёт: [docs/LG2_REPORT.md](LG2_REPORT.md).
- **LG3:** QueueDrawer/Sheet — `material-overlay` + no-blur scrim, open/out
  transitions, focus trap/return после outro; mobile/quick меню вынесены из
  chrome (без nested blur), Escape/outside close; 9 PIN-диалогов opt-in
  `material-scrim`/`material-overlay` + fade. Отчёт:
  [docs/LG3_REPORT.md](LG3_REPORT.md).

Скриншоты Stage 1: `docs/screenshots/stage1/`. Полный отчёт —
[docs/STAGE1_REPORT.md](STAGE1_REPORT.md).

## 1. Статус экранов

| Экран | Статус | Фаза |
|---|---|---|
| Home (Яндекс: лайки/плейлисты/волна/недавнее/загрузки) | реализован (mock) | P3 |
| Library (треки/альбомы/исполнители/локальные, импорт, фильтры) | реализован | P2 |
| Search (локальный) | поле поиска в Library (FTS5) | P2 |
| Search (Яндекс) | реализован (`/yandex/search`) | P3 |
| Setup (Device Flow/QR + manual token) | реализован | P3 |
| Lyrics / караоке | реализован | P5 |
| Wave card (like/dislike/skip) | реализован | P3 |
| Playlist / Album / Artist | реализован; плейлисты можно создавать/переименовывать/удалять, добавлять текущий трек | P3/P2 |
| Now Playing (обложка, караоке) | обложка из кэша + LyricsPanel | P1/P5 |
| Downloads | реализован (P4) | P4 |
| Offline shelf | реализован (P4) | P4 |
| История прослушивания | реализован (P1 hardening) | P1 |
| PWA (manifest + shell SW) | реализован; offline shell, без API/stream | P2 |
| Sync Center | реализован (P6) | P6 |
| Zones / Quick Panel | реализован (P7 mock) | P7 |
| Network / Wi-Fi | реализован (P8, nmcli/mock) | P8 |
| Settings | реализован (P8, громкость; sync read-only) | P8 |

Отсутствующие экраны не показываются в навигации и не имеют кнопок-заглушек
(AGENTS.md §12).

## 2. Палитра и токены (P1; Stage 1 tokens)

> Stage 1 ввёл `tokens.css`: `--glass-1..3`, `--stroke`, `--page-max`,
> `--dock-height`, type/motion tokens. Значения ниже — историческая P1-таблица;
> актуальные — в `frontend/src/styles/tokens.css` и `DESIGN_SYSTEM.md`.

| Токен | Значение | Использование |
|---|---|---|
| `--bg` | `#070a12` | фон |
| `--bg-soft` | `#0d1220` | панели |
| `--surface` | `rgba(255,255,255,.045)` | карточки |
| `--surface-hover` | `rgba(255,255,255,.075)` | hover |
| `--border` | `rgba(255,255,255,.08)` | границы |
| `--text` | `#eef2fb` | основной текст |
| `--muted` | `#8b93a7` | вторичный текст |
| `--accent` | `#8b5cf6` | акцент, play |
| `--accent-2` | `#22d3ee` | градиент, прогресс |
| `--danger` | `#f87171` | ошибки, удаление |
| `--radius` | `18px` | карточки |
| `--blur` | `18px` | матовое стекло |

Шрифт — локальный **Inter 500/600/700** (`frontend/src/assets/fonts/`, OFL),
fallback system stack; внешних CDN нет. Заголовки — 600–700, текст — 500.
Базовая сетка — 4/8 px; page tokens — в `tokens.css`.

Фон — full-bleed ambient cover + wash; обложки из `cover_url`, fallback —
градиент/инициалы. Blur отключается `prefers-reduced-transparency`/`?lite=1`.

## 3. Компоновка P1

```text
┌──────────────────────────────────────────────────────────────┐
│ MusicNX   [MOCK]                        ● WS online          │
├───────────────────────────────────────────────┬──────────────┤
│  ┌─────────────┐  Сейчас играет               │  Очередь     │
│  │   обложка   │  Название / исполнитель       │  ● трек      │
│  │  (градиент) │  бейдж доступности            │    трек      │
│  └─────────────┘  ▓▓▓▓▓▓░░░░ 1:23 / 3:34      │  …           │
│                   ⏮  ▶  ⏭   громкость        │  всего 12:04 │
├───────────────────────────────────────────────┤              │
│  Mock-полка                                   │              │
│  [карточка] [карточка] [карточка] [карточка]  │              │
└───────────────────────────────────────────────┴──────────────┘
```

- **Header** — логотип, навигация «Сейчас / Медиатека», режим (`MOCK`/`REAL`),
  индикатор WebSocket.
- **Now Playing** — обложка (из кэша или градиент), title/artists, бейдж
  доступности, прогресс (перетаскиваемый seek), play/pause, prev/next, громкость.
- **Полка** — карточки треков из `GET /library/tracks`; клик — play, кнопка «в
  очередь».
- **Очередь** — список с текущим элементом, удаление, суммарная длительность;
  пустое состояние с подсказкой.
- **Toast/предупреждение** — ошибки API и `system.warning` (ненавязчиво, сверху).

Мобильный (<860px): одна колонка, Now Playing сверху, очередь под полкой,
кнопки крупные (≥44px), без hover-зависимых действий.

## 3.1. Медиатека (P2)

```text
┌──────────────────────────────────────────────────────────────┐
│ Библиотека     [Импортировать] [Загрузить музыку]            │
│ [Готово ▓▓▓▓▓▓ 100%]  новых/обновлено/пропущено/ошибки      │
│ [Треки][Альбомы][Исполнители][Локальные]  поиск  доступность │
│ ┌ cover ┐ title / artist        album   duration  ● offline  │
│ ...                                                          │
│ Всего: N                                          [<] [>]    │
└──────────────────────────────────────────────────────────────┘
```

- Табы: треки; альбомы (сетка обложек, клик → треки этого альбома); исполнители
  (аватары/инициалы, клик → треки); локальные файлы (фильтр `offline`).
- Тулбар: поиск (FTS5 для треков, LIKE для альбомов/исполнителей), фильтр
  доступности, пагинация 50/страница.
- Импорт: кнопка запускает job по `library.import_roots`; панель прогресса
  показывает фазу, проценты, counts, ошибки; кнопка отмены. После
  `library.updated` список и полка перечитываются.
- Импорт/upload: «Импортировать» сканирует `library.import_roots`; «Загрузить
  музыку» принимает несколько файлов с прогрессом. PIN не используется:
  мутации открыты в LAN (PIN removed, ADR-EX-001).
- Обложки: `cover_url` 300px в списках, 1000px в Now Playing, 64px — фон/превью;
  64px pre-blur. Если обложки нет — градиент с инициалами.
- Ошибки импорта не скрываются: последние 20 сообщений в job; панель показывает
  первые три.

## 3.2. Яндекс (P3)

- Навигация «Яндекс»: Home с полками «Мне нравится», «Плейлисты», «Моя волна»,
  честными «Недавнее» (P3+) и «Загрузки» (P4); неавторизованному показывается
  кнопка подключения.
- Setup: Device Flow (QR из backend, user_code, verification_url, таймер),
  poll статуса, ручной токен, logout; PIN-диалога нет (PIN removed). Токен во
  frontend не хранится.
- Now Playing: для `source=yandex` — обложка, бейдж `30 сек` для preview,
  очередь, управление; browser zone включает `<audio src="/stream/{id}">`
  только при `audio.browser_zone=true`.
- Wave card: старт станции, like/dislike/skip; после действия сервер отдаёт
  обновлённый WaveState (refill на сервере).
- Мобильный: те же правила <860px; QR и кнопки крупные.

## 3.3. Загрузки (P4)

- Навигация «Загрузки»: очередь и история, status/format/bytes/attempts/error,
  cancel/retry/delete; polling 1 с только при активных задачах.
- Кнопка «Скачать» на Яндекс-треках/альбомах/плейлистах; после `download.done`
  трек получает offline-бейдж, OfflineShelf перечитывается.
- Офлайн-полка в «Сейчас»: только verified файлы; пусто — честное объяснение.
- Формат-бейдж фактический (`m4a`, `flac`, `mp3`), не обещанное качество.

## 3.4. Тексты (P5)

LyricsPanel под Now Playing: skeleton/нет текста/ошибка; LRC подсвечивает
активную строку по `position + offset`, автоскролл (reduced-motion), клик/Enter
по строке → seek; offset ±0.5 с сохраняется PUT (mutation; PIN removed); plain —
прокручиваемый текст. `lyrics.updated` обновляет панель во всех вкладках.

## 3.7. CardsNX design pass

![Before: monograms and dev copy](screenshots/design-now-before.png)

![After: real covers, QuickBar and cover cards](screenshots/design-now-after.png)

![Fullscreen karaoke](screenshots/karaoke-lrc.png)

- **Covers:** every shelf/queue/Now Playing/offline row uses `CoverArt` with a real
  `<img>` from `/artwork` or `/yandex/cover` (307 → `/artwork/{key}`); gradient/
  monogram is only a load/error fallback. Yandex album covers are persisted to
  `albums.cover_path`, so player/library DTOs keep `cover_url` after restart.
- **Ambient/glass:** pre-blurred 64px cover fills a fixed ambient layer; panels use
  translucent gradients + `backdrop-filter`; one accent color from the artwork.
- **Cover-forward cards:** art fills the card, title/artist/duration in a bottom
  gradient, availability and queue actions in corners, arrow rail + scroll-snap.
- **QuickBar:** sticky mini-control with volume, zone selector, BT/Wi-Fi status,
  sleep timer; power is explicitly shown as Pi-only (no fake API).
- **Karaoke fullscreen:** separate overlay from Now Playing («Текст · L»/L/Esc),
  left cover/title/progress, right LRC with bright current line, distance dimming,
  auto-scroll pause on manual scroll, click line → seek; plain fallback.
- **Lightweight mode:** `?lite=1` / `prefers-reduced-transparency` disables blur;
  `prefers-reduced-motion` disables transitions/scroll animation.
- Dev copy (WS/mpv explanation, queue synchronization footnote, phase chips) is
  removed; footer only shows version.

## 4. Поведение и синхронизация

- При загрузке — WS-снапшот; до него — скелетоны (не «0:00»).
- Все действия — REST; оптимистичного изменения состояния нет: ждём событие/ответ.
  Кнопки блокируются на время запроса, чтобы не плодить гонки.
- `player.position` приходит 2 Гц только видимой вкладке; между событиями UI
  интерполирует по `performance.now()` от `server_time_ms` (ADR-007).
- `visibilitychange` → `client.hello {visible}`.
- Реконнект WS — exponential backoff 0.5 → 1 → 2 → 5 с, статус в header.
  После реконнекта снапшот заменяет локальное состояние.
- Две вкладки: события применяются обеими; никакого localStorage-состояния.

## 5. Доступность и клавиатура

- Управление: `Space` — play/pause, `←`/`→` — seek ∓5 с (когда фокус не в
  поле ввода), `N`/`P` — next/previous. Подсказка в футере Now Playing.
- Все кнопки — `<button>` с `aria-label`; прогресс — `input[type=range]` с
  `aria-valuetext`; индикатор WS имеет текстовый статус, не только цвет.
- Контраст текста ≥ 4.5:1 на фоне; видимый focus ring.
- `prefers-reduced-motion` — отключить переходы/анимации.

## 6. Данные и состояния

| Состояние | Отображение |
|---|---|
| Нет трека | «Ничего не играет», полка активна |
| `buffering` | спиннер на play-кнопке, позиция не тикает |
| `error` | красный бейдж «Ошибка воспроизведения», тост с текстом |
| Очередь пуста | подсказка «Добавьте трек с полки» |
| Полка пуста | «Библиотека пуста» (в mock — seed; в real — импорт) |
| Импорт идёт | панель прогресса, кнопка «Отмена», списки доступны |
| Импорт/upload | прогресс; PIN не используется, мутации открыты в LAN |
| Обложки нет | градиент с инициалами (не пустая карточка) |
| Нет WS | «Нет связи, переподключение…», действия доступны через REST |
| MOCK | бейдж в header + подпись «виртуальное воспроизведение» |

## 7. Стиль CardsNX: что берём и что нет

Берём: тёмный фон, стеклянные карточки, крупные обложки, мягкие тени,
горизонтальные полки, акцентный градиент, спокойные микропереходы.
Не берём: тяжёлые UI-библиотеки, canvas-визуализаторы, blur на каждом элементе
(слабые ТВ), автоплей-видео, внешние шрифты/иконки по CDN.

## 8. Технические детали (P1/P2)

- Svelte 5 + Vite + TS, без UI-фреймворков и CSS-препроцессоров.
- Сборка: `pnpm --dir frontend build` → `backend/app/static`; backend отдаёт SPA.
- Dev: Vite `:5173`, proxy `/api` (включая WS) на `:8080`.
- Иконки — inline SVG (компонент `Icon.svelte`).
- Никаких токенов/секретов в frontend и localStorage.
