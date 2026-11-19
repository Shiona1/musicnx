# Развёртывание на Raspberry Pi

Целевая платформа — Raspberry Pi 3 Model B, Raspberry Pi OS Lite. 64-bit
рекомендуется; 32-bit `armv7l` на Pi 3 (1 ГБ) допускается при проверке нативных
зависимостей (ADR-019, SPEC §2). Node.js на плате не нужен: frontend
собирается на dev-машине в `backend/app/static`. Docker не требуется.
**Статус:** постоянный systemd user service установлен и проверен на Pi
(2026-11-14), `install.sh`/update/backup — P8. Real-import на Pi **ожидает
замены БП** (under-voltage, docs/HARDWARE.md §9); real playback P1 проверен.

## 1. Требования

- Raspberry Pi 3 Model B (1 ГБ RAM), 4 ядра.
- SD-карта ≥16 ГБ (A1/A2, промышленная — лучше), либо SD + USB-накопитель для музыки.
- Питание 5 В / 2.5 А (недостаток питания — причина «залипаний» и WiFi-сбоев).
  **Проверка:** `vcgencmd get_throttled` должен быть `0x0`; на тестовом Pi сейчас
  `0x50005` (under-voltage) — заменить БП до real-import/soak.
- Ethernet (приоритет) или Wi-Fi 2.4 ГГц; Bluetooth-аудио рекомендуется с Ethernet.
- Raspberry Pi OS Lite; 64-bit рекомендуется, 32-bit `armv7l` допускается
  (проверено на Trixie 32-bit, Python 3.13.5).
- `gpu_mem=16` в `/boot/firmware/config.txt` (нет экрана, экономия RAM).

## 2. Пакеты

```bash
sudo apt update
sudo apt install -y mpv pipewire pipewire-pulse wireplumber \
  pulseaudio-utils libspa-0.2-bluetooth bluez avahi-daemon \
  network-manager sqlite3 curl
sudo systemctl enable --now avahi-daemon
```

`pipewire-pulse` даёт PulseAudio-совместимый интерфейс для mpv. `wpctl`/`pactl`
используются роутером зон (P7).

## 2.1 TLS (optional for LAN)

v1 is LAN-only on port 8080; TLS is **not required** and no certificate is part
of acceptance (ADR-035). If the owner exposes MusicNX through a reverse proxy,
terminate TLS there (Caddy/nginx) and keep 8080 bound to LAN/loopback:

```caddy
musicnx.example.lan {
    reverse_proxy 127.0.0.1:8080
}
```

Then set `MUSICNX_SERVER__HOST=127.0.0.1` (or firewall 8080 to the proxy) and use
`https://`; do not expose raw 8080 to the internet. Cookies become `Secure`
automatically when the request scheme is HTTPS.

## 3. Пользователь и каталоги

Пользователь и hostname — параметры (дефолт `musicnx`; существующая установка
под другим именем допустима, ADR-020). Пример для дефолта:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin musicnx
sudo loginctl enable-linger musicnx        # PipeWire как user-сервисы без логина
sudo mkdir -p /opt/musicnx /var/lib/musicnx/{database,library,cache,downloads,backups}
sudo chown -R musicnx:musicnx /opt/musicnx /var/lib/musicnx
```

Раскладка:

```text
/opt/musicnx/
├── app/                    # копия backend/ (app/, requirements.txt, pyproject.toml)
│   └── app/static/         # собранный frontend (копия backend/app/static)
├── venv/
└── config/config.yaml      # конфиг (вне app, пути абсолютные)
/var/lib/musicnx/
├── database/musicnx.db
├── library/
├── cache/{covers,tmp}
├── downloads/
└── backups/
```

Пакеты Python ставятся из `backend/requirements.txt` (экспорт из `uv.lock`,
точные версии). Node на Pi не нужен.

Пути задаются в `config/config.yaml` (`paths.*`) — перенос музыки на USB не
требует изменения кода.

## 4. Установка backend и frontend

На dev-машине:

```bash
make build                      # frontend → backend/app/static
pwsh -File scripts/build-release.ps1  # dist/musicnx-app-YYYYMMDD.tgz:
                                      # backend/app + requirements.txt + pyproject.toml
```

Deploy-скрипты (`scripts/deploy-pi*.ps1`) берут последний bundle, распаковывают
его и выполняют `venv/bin/python -m pip install -r backend/requirements.txt` +
`pip check` до restart. `python-multipart` и остальные runtime-зависимости не
нужно ставить руками.

На Pi:

```bash
sudo mkdir -p /opt/musicnx/config
sudo chown -R musicnx:musicnx /opt/musicnx
# скопировать backend/ (включая app/static) в /opt/musicnx/app
# скопировать config/config.example.yaml в /opt/musicnx/config/
cd /opt/musicnx
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r app/requirements.txt

sudo -u musicnx cp /opt/musicnx/config/config.example.yaml /opt/musicnx/config/config.yaml
# отредактировать пути на /var/lib/musicnx/..., mode: real, audio.backend: mpv,
# library.import_roots (папки с музыкой). PIN удалён; мутации в LAN открыты,
# не публикуйте порт в интернет (docs/PIN_REMOVAL.md).
chmod 600 /opt/musicnx/config/config.yaml
```

До P8 `install.sh` не автоматизирован. Запуск вручную:

```bash
cd /opt/musicnx/app
sudo -u musicnx env MUSICNX_CONFIG=/opt/musicnx/config/config.yaml \
  /opt/musicnx/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Проверить: `curl -s http://127.0.0.1:8080/api/v1/system/health`.

Альтернатива для разработки — `uv sync` на Pi, но для продакшена достаточно
`requirements.txt`; uv на плате не обязателен. Нативные wheels для `armv7l`
(pydantic-core, uvloop, httptools, pillow, mutagen) доступны через piwheels;
`/etc/pip.conf` на Raspberry Pi OS уже добавляет piwheels.

## 5. systemd user service (реализовано и проверено 2026-11-14)

Используется параметризованный пользователь (дефолт `musicnx`; проект обычно в
`$HOME/musicnx` или `/opt/musicnx`). Юнит
`~/.config/systemd/user/musicnx.service`:

```ini
[Unit]
Description=MusicNX media server
After=network-online.target pipewire.service wireplumber.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/musicnx/app
Environment=PYTHONUNBUFFERED=1
Environment=MUSICNX_CONFIG=/opt/musicnx/config/config.yaml
Environment=MUSICNX_AUDIO__IPC_SOCKET=%t/musicnx/mpv.sock
ExecStartPre=/usr/bin/mkdir -p %t/musicnx
ExecStart=/opt/musicnx/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=2
TimeoutStopSec=20
KillSignal=SIGTERM

[Install]
WantedBy=default.target
```

`%t` — runtime-каталог пользователя (`/run/user/<uid>`), не хардкод
`/run/user/1000`. mpv-сокет живёт в `%t/musicnx/mpv.sock`; `ExecStartPre`
создаёт каталог. MPV — дочерний процесс backend; отдельный юнит не нужен.

```bash
systemctl --user daemon-reload
systemctl --user enable --now musicnx.service
systemctl --user status musicnx.service
```

`loginctl enable-linger <user>` обязателен, иначе user-сервис остановится при
выходе сессии. На тестовом Pi выполнен `enable-linger` и `enable --now musicnx`;
после `reboot` сервис поднялся сам, UI ответил, очередь/трек восстановились в
статусе `paused` (ADR-004), `gpu_mem=16` применился. `install.sh`,
`musicnx-maintenance.timer`, update/backup — P8.

### 5.1 Импорт медиатеки (P2)

- `library.import_roots` — список существующих папок с музыкой; пусто =
  `paths.library`. Импорт не выходит за эти корни.
- PIN удалён: импорт/upload с любого LAN-клиента разрешены. Не публикуйте порт
  в интернет; см. docs/PIN_REMOVAL.md.
- UI: «Медиатека» → «Импортировать»/«Загрузить музыку»; прогресс и ошибки видны.
- Real-import/upload проверены; полная нагрузка ограничена under-voltage —
  сначала заменить БП (docs/HARDWARE.md), затем импортировать папку.

### 5.2 Яндекс Музыка (P3)

- `yandex.enabled: true` в `config/config.yaml`; `yandex.quality` — `mp3`
  (по умолчанию), `aac` или `flac`.
- `yandex.token_file` — абсолютный путь вне web root, например
  `/opt/musicnx/var/secrets/yandex_token.json`; файл создаётся с правами 0600.
- Device Flow: UI «Яндекс» → «Подключить Яндекс» → QR/код. `POST
  /yandex/auth/device/start` — mutation (PIN removed, LAN open).
- Ручной токен — fallback через тот же экран; токен не попадает в браузер.
- `audio.browser_zone: true` — mpv остаётся клоком, локальный звук muted,
  клиент играет `/stream/{track_id}`. Для Pi с колонками оставить `false`.
- Real-smoke на Pi — только при отсутствии активных throttling-битов; короткий
  streaming-smoke допустим, import/checksum/soak — нет.

## 6. Сеть

- Ethernet приоритетнее Wi-Fi: NetworkManager connection `ipv4.route-metric 100`
  для eth0 и `600` для wlan0.
- Несколько Wi-Fi-профилей — NetworkManager сам выбирает доступный.
- avahi → `http://<hostname>.local:8080` на обоих интерфейсах (дефолт
  `musicnx.local`; существующая установка под другим именем допустима).
- Проверка: `avahi-browse -rt _http._tcp`, `getent hosts <hostname>.local`.
- AP-onboarding (`MusicNX-Setup`) — P8, не реализован.

## 7. Производительность и SD

- `gpu_mem=16`; отключить неиспользуемые сервисы (desktop отсутствует).
- `noatime` в `/etc/fstab` для root и USB-раздела музыки — меньше записи.
- Логи: journald с лимитом (`SystemMaxUse=50M`) или файл с ротацией.
- Кэш обложек на SD ограничить; загрузки — на USB (предупреждение в UI).
- Не сканировать библиотеку по таймеру (P2: по событию/вручную).
- Позиция плеера — debounce 7 с (не убивать SD записями).

## 8. Обновление и бэкап (P8)

```bash
# update.sh: остановить сервис, распаковать новый релиз, миграции при старте,
# проверить health, при ошибке — откат на предыдущий каталог
# backup.sh: sqlite3 .backup или Backup API, ротация, копия на USB/NAS
```

Восстановление: остановить сервис, положить файл БД на место, запустить,
`GET /system/health` и `PRAGMA quick_check`. Не копировать активный WAL-файл
вслепую (SPEC §11.7).

## 9. Диагностика

| Проблема | Команда/причина |
|---|---|
| сервис не стартует | `journalctl --user -u musicnx -n 100` |
| нет звука | `wpctl status`, `pactl info`, `mpv /path/test.flac` |
| mpv не найден | `which mpv`, `audio.mpv_path` в конфиге |
| UI не открывается | `ss -lntp | grep 8080`, firewall, mDNS |
| БД заблокирована | не запущено ли два процесса; `busy_timeout` |
| заикания/перезагрузки | `vcgencmd get_throttled` → заменить БП/кабель |
| импорт/upload 401/403 | PIN удалён; проверьте Origin/Host, а не PIN |
| обложек нет | файл без embedded art; смотрите лог `musicnx.library` |
| высокая температура | `vcgencmd measure_temp`, корпус/радиатор, throttling |
| мало RAM | `free -m`, `ps aux --sort=-rss`, отключить лишнее |

## 10. Ограничения текущего состояния

- Постоянный `musicnx.service` установлен и проверен reboot-ом; `update.sh`,
  `backup.sh`, `restore.sh`, `build-release.ps1` есть; `install.sh` — вне v1.
- Real-import/upload проверены; полная нагрузка — pending до замены БП
  (`throttled=0x50005`); mock-импорт, FTS, обложки, dedupe проверены.
- Токены Яндекса/стриминг/загрузки/зоны — P3/P4/P7; на P2 real-режим играет
  локальные файлы; PIN удалён, LAN-мутации открыты.
- Браузер на Pi не запускается и не устанавливается.
