# Аудио: mpv, PipeWire, зоны, browser sync

Требования — [SPEC.md](../SPEC.md) §4, §7.3, §7.4. Реализовано на P1: mpv IPC,
mock-backend, громкость, базовый state machine. PipeWire/зоны/BT/browser zone —
P3/P7; здесь зафиксирован контракт, чтобы код не пришлось переделывать.

## 1. Принципы (жёсткие)

1. **mpv — мастер-клок.** Позиция трека всегда = позиция mpv. Никаких вторых часов.
2. **mpv — subprocess + JSON IPC** через unix socket. Не python-mpv, не libmpv.
3. **Никакого транскодирования.** mpv играет файл/URL как есть; `/stream` в P3
   отдаёт оригинальные байты с Range.
4. **Python не пропускает аудиобайты** локального вывода.
5. Один активный выход в MVP (P7 добавляет переключение зон).
6. Позиция mpv не пишется в SQLite каждый тик (debounce, ADR/DB docs).
7. Все subprocess — аргументами-списком, без shell.

## 2. Запуск mpv (real-режим)

Backend сам запускает mpv с аргументами:

```bash
mpv --idle=yes --no-video --no-terminal \
    --input-ipc-server="$XDG_RUNTIME_DIR/musicnx/mpv.sock" \
    --volume=70
```

Вручную mpv запускать **не нужно**: второй процесс будет конкурировать за сокет.
Команда выше приведена для диагностики. Процесс — дочерний у backend
(`asyncio.create_subprocess_exec`), отдельный systemd-юнит не обязателен
(SPEC §12). Backend перезапускает mpv при падении.

Конфиг (`config/config.yaml`):

```yaml
audio:
  backend: mpv            # auto | mpv | mock
  mpv_path: mpv
  # ipc_socket: /run/user/<uid>/musicnx/mpv.sock   # YAML не раскрывает $XDG_RUNTIME_DIR
  start_timeout_s: 10
  restart_backoff_s: [1, 2, 5]
```

Если `ipc_socket` не задан, backend берёт путь из `$XDG_RUNTIME_DIR`, а при его
отсутствии — `/run/user/<uid>` (иначе системный temp). Для systemd user service
сокет задаётся через runtime-каталог пользователя, без хардкода uid:

```ini
Environment=MUSICNX_AUDIO__IPC_SOCKET=%t/musicnx/mpv.sock
ExecStartPre=/usr/bin/mkdir -p %t/musicnx
```

`%t` раскрывается в `/run/user/<uid>`; на тестовом Pi это
`/run/user/1000/musicnx/mpv.sock`. Каталог создаётся до запуска backend.

`auto` на POSIX выбирает mpv, если бинарь найден; иначе mock с предупреждением
в логе и `audio.backend` в `/system/status`. На Windows `real` не поддерживается
в P1 (named pipes) — ошибка конфигурации, см. ADR-006.

## 3. JSON IPC

Транспорт — `MpvTransport` (`app/audio/mpv_ipc.py`), реализация
`UnixSocketTransport`. Логика контроллера не зависит от транспорта и тестируется
на fake-транспорте без mpv (`test_mpv_ipc.py`).

Формат: по одному JSON-объекту на строку в обе стороны.

**Команда** (клиент → mpv):

```json
{"command": ["loadfile", "/music/track.flac", "replace"], "request_id": 17}
```

**Ответ**:

```json
{"request_id": 17, "error": "success", "data": null}
```

Ошибка: `"error": "loading failed"` → `MpvCommandError` с исходным кодом.
Таймаут ожидания ответа — 5 с, pending future отменяется.

**Событие** (mpv → клиент):

```json
{"event": "property-change", "id": 1, "name": "time-pos", "data": 42.531}
```

### Команды и подписки P1

| Действие | Команда mpv |
|---|---|
| загрузить | `["loadfile", uri, "replace"]` |
| пауза | `["set_property", "pause", true]` |
| продолжить | `["set_property", "pause", false]` |
| остановить | `["stop"]` |
| seek | `["seek", <seconds>, "absolute"]` |
| громкость | `["set_property", "volume", 0..100]` |
| подписка | `["observe_property", id, "time-pos"]`, `duration`, `idle-active`, `eof-reached` |

`time-pos`/`duration` кэшируются в backend (секунды → мс). `eof-reached=true`
ставит флаг EOF, который обрабатывает PlayerManager (next или stopped).
Отписка не требуется: соединение живёт вместе с backend, при рестарте mpv
подписки выдаются заново.

### Загрузка файла и seek (важно)

`loadfile` отвечает успехом **до** того, как файл готов к seek. Немедленный
`seek` после него mpv отклоняет (`error running command`). Поэтому порядок:

```text
loadfile → ждать событие file-loaded → pause yes → seek (если start_ms > 0)
```

`MpvIpcClient` слушает `file-loaded` и выставляет `asyncio.Event`; backend ждёт
его с таймаутом `audio.start_timeout_s`. Это делает надёжными resume с позиции,
переключение трека и seek сразу после старта (проверено на Pi 3).

Событие `end-file` приходит и при замене файла (`reason: "stop"`), и при
настоящем конце (`reason: "eof"`). EOF-флаг ставится **только** для `eof`,
иначе перезагрузка трека сразу останавливала бы воспроизведение.

Если метаданные трека не содержат длительности (например, dev-фикстура без
mutagen), backend берёт `duration` из mpv после загрузки и публикует обновлённое
состояние; UI получает длительность из `PlayerState.duration_ms`.

Когда `idle-active=true`, mpv готов к загрузке. Загрузка: `loadfile` →
`set_property pause false` → при необходимости `seek <start_s> absolute`.

## 4. Supervisor и восстановление

- `MpvAudioBackend` держит `MpvController` и фоновую задачу супервизора.
- Если процесс умер: статус backend `not ready`, WS `system.warning`
  (`mpv_restarted`), backoff 1 → 2 → 5 с (далее 5 с), перезапуск, реконнект IPC.
- После реконнекта, если был текущий URI: `loadfile`, `seek` на последнюю
  известную позицию, `pause` — по текущему статусу PlayerManager
  (`playing` → продолжить). Ошибки не глотаются: после 5 неудач подряд статус
  плеера `error`, событие `system.warning` с кодом `mpv_unavailable`.
- При shutdown backend: остановить супервизор, `quit` (best effort), terminate,
  wait с таймаутом, kill.

## 5. Громкость

- Диапазон 0..100, целое (ADR-010).
- Хранится в `settings` (`player.volume`) и `playback_state.volume`.
- На P7 — per-zone (`audio_zones.volume`), mpv получает громкость активной зоны;
  BT-зона дополнительно может управляться на стороне BlueZ.

## 6. Зоны (контракт; реализация P7)

| kind | sink | приоритет | offset по умолчанию |
|---|---|---|---|
| `local_sink` | jack 3.5mm (bcm2835) | 2 | 0 |
| `local_sink` | USB-DAC | 1 (появляется → приоритетнее) | 0 |
| `bt` | A2DP sink | 3 | 150–300 мс (настраивается) |
| `browser` | виртуальная | — | 0 (P3) |

- Переключение — перенос sink-input mpv на лету (`pactl move-sink-input`),
  без остановки трека; громкость применяется к новому sink.
- Управление PipeWire: `pactl -f json`, `wpctl`, аргументы списком.
- BT: `bluetoothctl`/D-Bus, SBC по умолчанию, авто-reconnect, «забыть».
- Если BT + Wi-Fi 2.4 ГГц → UI-подсказка «рекомендуется Ethernet» (SPEC §2).
- `delay_offset_ms` применяется к караоке (P5), не к аудио-синхронизации P1.

## 7. Browser zone (P3)

- mpv остаётся мастер-клоком; при активной browser-зоне локальный sink mpv
  **замьючен, но не остановлен**.
- `GET /api/v1/stream/{track_id}`:
  - локальный файл — отдача с Range;
  - Яндекс CDN — форвард Range на CDN, без транскодирования;
  - `206 Partial Content`, `Accept-Ranges: bytes`, корректный `Content-Type`
    исходного кодека.
- Клиент: `<audio>` стартует с серверной позиции; каждые N секунд сверяет
  `currentTime` с позицией из WS; дрейф > 0.5 с → ресинк (`currentTime = pos`).
- Двойной трафик в MVP допустим (SPEC §4); общий дисковый кэш — позже.
- Токены/подписанные URL не попадают во frontend: `/stream` ходит на CDN сам.

## 8. Mock backend (P0/P1)

`MockAudioBackend`:

- позиция вычисляется по `time.monotonic()` от `_base_ms` при `playing`;
- `load(uri, start_ms)` запоминает uri и стартовую позицию; длительность
  приходит из `TrackDTO` (шаблон URI `mock://track/{id}?duration_ms=…`);
- `pause` фиксирует позицию, `play` продолжает, `seek` меняет базу,
  `stop` сбрасывает;
- EOF определяет PlayerManager по `position_ms >= duration_ms`;
- `ready` всегда true, `describe()` → `{backend: "mock", ready: true}`.

Звука нет — это явно показано в UI бейджем «MOCK». Mock не эмулирует mpv-команды
и не притворяется реальным выводом.

## 9. Диагностика

**Fallback источника.** Если трек не удалось открыть (`no_playable_source`,
`source_unreachable`, `stream_unavailable`, `cdn_unreachable`),
`PlayerManager` один раз ищет замену через `TrackFallbackService`: сначала
локальный дубль (то же название/исполнитель, есть файл), затем другая версия в
каталоге Яндекса. Найденный трек подменяет текущий элемент очереди, плеер
продолжает с начала и публикует `system.warning track_fallback`; UI показывает
тост. Если CDN помечен недоступным, онлайн-поиск не выполняется. Это не
скачивание с YouTube/VK/SoundCloud: проект не обходит авторские права/ToS.

**Sink watchdog.** `ZoneManager` раз в 15 с сверяет активную зону с реальными
sink'ами PipeWire. Если активный BT/USB-sink исчез (колонка выключилась,
WirePlumber перезапустился), сервер не рестартит mpv: он публикует
`system.warning audio_sink_lost` и переключает активную зону на local. mpv
supervisor отдельно отвечает за смерть самого mpv.

**Автопереход и недоступный источник.** На EOF при `player.autoplay=true`
`PlayerManager` играет следующий доступный трек. Если источник ответил
`cdn_unreachable`, CDN считается недоступным на 5 минут: остальные онлайн-треки
в очереди пропускаются без повторного 3-секундного таймаута. Когда доступных
треков не осталось, плеер останавливается и публикует `system.warning`
`queue_exhausted_offline`, а не уходит в `error`. Явное нажатие Play на
онлайн-треке по-прежнему показывает ошибку (автопропуска нет). Флаг живёт в
памяти процесса, не персистится и сбрасывается успешной загрузкой удалённого
потока; рестарт сервиса снимает его досрочно.

| Симптом | Проверка |
|---|---|
| нет звука на Pi | `systemctl --user status musicnx`, `pactl info`, `wpctl status` |
| mpv не стартует | `mpv --version`, права на `/run/user/<uid>/musicnx`, лог backend |
| IPC timeout | сокет существует? процесс жив? не перезаписан ли путь |
| треск/заикание BT | Wi-Fi 2.4 ГГц рядом; Ethernet; SBC-XQ выключен |
| позиция скачет | не запущено ли два backend; `ps aux | grep uvicorn` |
| BT: `br-connection-profile-unavailable` / `Protocol not available` | headless-сессия и WirePlumber seat monitoring: выполнить `sh scripts/fix-bluetooth-headless.sh`, затем переподключить колонку |
| BT: `br-connection-key-missing` после успешного pair | устаревший bond: `bluetoothctl remove <MAC>`, включить pairing на колонке, заново `pair` + `trust` + `connect` |
| BT подключён, но колонки нет в зонах | `pactl list short sinks`; A2DP-sink появляется только после исправления seat monitoring |
| всё медленно, треск, обрывы BT | `vcgencmd get_throttled` (не `0x0`), `dmesg` (искать `undervoltage`); заменить БП/кабель на 5 В ≥2,5 А |
| браузер-зона молчит | в `GET /system/status` `audio.browser_zone=true`; `/stream/{id}` открывается с Range; браузеру нужно разрешение на автозвук |
| Яндекс-трек: `source_unreachable`, `/stream` 503 `cdn_timeout` | `CdnRouter` не нашёл рабочий edge; посмотреть `grep "CDN edge" var/log/musicnx.log`, проверить сеть/провайдера |
| очередь остановилась с `queue_exhausted_offline` | дальше только онлайн-треки, CDN помечен недоступным; проверить сеть, подождать 5 минут или перезапустить сервис |
| страница не скроллится после караоке/очереди | обновить фронтенд: блокировка скролла теперь один владелец (`html.scroll-locked`), старый inline `body.overflow` снимается при загрузке |



Логи — `var/log/musicnx.log` (dev) / journald (Pi). Токены и URL с подписью
в лог не попадают (redaction).
