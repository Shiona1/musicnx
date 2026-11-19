# Эксплуатация MusicNX (P8)

Документ описывает реальные операции на Raspberry Pi: установка/обновление,
бэкап, восстановление, systemd, логи, rollback и soak-тест. Источник истины по
требованиям — [SPEC.md](../SPEC.md) §13, по установке — [DEPLOYMENT.md](DEPLOYMENT.md).
Скрипты: [`scripts/backup.sh`](../scripts/backup.sh),
[`scripts/restore.sh`](../scripts/restore.sh),
[`scripts/update.sh`](../scripts/update.sh),
[`scripts/soak.sh`](../scripts/soak.sh).

**Статус P8:** операции покрыты CLI-скриптами; страница Wi-Fi/AP-onboarding
есть, TLS и OTA **не реализованы** (см. §11). PIN удалён владельцем: LAN-мутации
открыты (residual risk, ADR-EX-001). Живого restore API нет by design.
`/var/lib/musicnx` — дефолт; при установке проекта в другой каталог
пути переопределяются переменными (ниже).

## 1. Соглашения

- Все скрипты — POSIX `sh`, запускаются **от пользователя сервиса**, не от root.
- Скрипты ничего не читают из `config.yaml` автоматически: передавайте пути
  через переменные окружения (таблица ниже). Проверить фактические пути:

  ```bash
  systemctl --user cat musicnx            # WorkingDirectory, ExecStart, MUSICNX_CONFIG
  systemctl --user show -p Environment musicnx
  ls -l "$HOME/musicnx/var/musicnx.db"    # или /var/lib/musicnx/database/musicnx.db
  ```

- `sqlite3` обязателен для backup/restore/update:
  `sudo apt install sqlite3 curl` (уже есть в §2 DEPLOYMENT).
- `systemctl --user` управляет юнитом; `--no-service` — только для dev, БД
  должна быть остановлена вручную.

| Переменная | Скрипты | Назначение | Дефолт |
|---|---|---|---|
| `MUSICNX_DB` | backup, restore, update | файл БД | `/var/lib/musicnx/database/musicnx.db` |
| `MUSICNX_BACKUP_DIR` | backup, restore, update | каталог бэкапов | `/var/lib/musicnx/backups` |
| `MUSICNX_BACKUP_KEEP` | backup | сколько снапшотов хранить | `14` |
| `MUSICNX_BACKUP_COPY_DIR` | backup | копия на USB/NAS (без ротации) | — |
| `MUSICNX_APP_DIR` | update | каталог с `app/main.py` | `/opt/musicnx/app` |
| `MUSICNX_VENV` | update | venv | `/opt/musicnx/venv` |
| `MUSICNX_CONFIG` | update | конфиг для миграций | `/opt/musicnx/config/config.yaml` |
| `MUSICNX_SERVICE` | restore, update, soak | systemd user unit; пусто = отключено | `musicnx` |
| `MUSICNX_HEALTH_URL` | restore, update | health endpoint | `http://127.0.0.1:8080/api/v1/system/health` |
| `MUSICNX_HEALTH_TIMEOUT` | restore, update | ожидание health, с | `30` |
| `MUSICNX_ROLLBACK_DIR` | update | снапшоты кода | `<parent APP_DIR>/rollbacks` |
| `MUSICNX_BASE_URL` | soak | база API | `http://127.0.0.1:8080` |
| `MUSICNX_SOAK_DIR` | soak | CSV/лог | `/var/lib/musicnx/soak` |
| `MUSICNX_SOAK_INTERVAL` | soak | интервал опроса, с | `30` |

Пример окружения для текущего Pi (подставьте реальные пути из `systemctl cat`):

```bash
export MUSICNX_DB="$HOME/musicnx/var/musicnx.db"
export MUSICNX_BACKUP_DIR="$HOME/musicnx/var/backups"
export MUSICNX_APP_DIR="$HOME/musicnx/backend"
export MUSICNX_VENV="$HOME/musicnx/venv"
export MUSICNX_CONFIG="$HOME/musicnx/config/config.yaml"
export MUSICNX_SERVICE=musicnx
```

## 2. Установка (вручную; install.sh в этом наборе нет)

Базовая установка — [DEPLOYMENT.md](DEPLOYMENT.md) §2–5. Скрипты кладутся рядом
с приложением:

```bash
sudo mkdir -p /opt/musicnx/scripts
sudo cp scripts/{backup,restore,update,soak}.sh /opt/musicnx/scripts/
sudo chmod 755 /opt/musicnx/scripts/*.sh
sudo chown -R musicnx:musicnx /opt/musicnx/scripts
```

При проекте в `$HOME/musicnx` достаточно хранить скрипты в репозитории
и запускать от пользователя сервиса; executable-бит:

```bash
chmod +x scripts/*.sh
```

Проверка: `sh scripts/backup.sh --help`, `sh scripts/soak.sh --help`.

## 3. systemd user service

Юнит и его проверка — [DEPLOYMENT.md](DEPLOYMENT.md) §5. Кратко:

```bash
systemctl --user daemon-reload
systemctl --user enable --now musicnx.service
systemctl --user status musicnx.service
loginctl enable-linger "$USER"        # без linger user-сервис умрёт с сессией
```

`systemctl --user` работает только в сессии пользователя сервиса. Если скрипт
запущен через `sudo`, user-bus недоступен — скрипты откажутся работать
(restore/update) и попросят `--no-service`.

## 4. Бэкап

Онлайн-снимок через `sqlite3 .backup` (корректно при работающем сервисе),
`PRAGMA integrity_check`, режим `0600`, ротация `musicnx-*.db` до 14 файлов.

```bash
# обычный запуск (пути из окружения)
sh scripts/backup.sh

# явные пути
sh scripts/backup.sh --db "$MUSICNX_DB" --dir "$MUSICNX_BACKUP_DIR" --keep 14

# копия на USB/NAS (каталог должен быть смонтирован и доступен на запись)
sh scripts/backup.sh --copy-to /mnt/usb/musicnx-backups

# проверить снимок вручную
sqlite3 "/var/lib/musicnx/backups/musicnx-20261114-230000.db" "PRAGMA integrity_check;"
```

Что **не** попадает в снимок: `config/config.yaml` (там могут быть секреты), файл
токена Яндекса, `.env`, медиафайлы, `.log`. В БД лежат библиотека, история,
настройки приложения; токен Яндекса — отдельный файл `0600` вне web root.
Поэтому бэкап БД не заменяет бэкап токена (его не нужно копировать в открытом
виде) и не содержит секретов конфигурации.

Ротация управляет только `musicnx-*.db`. Файлы `pre-restore-*.db` (создаёт
restore.sh) и ручные копии с другим префиксом не удаляются; удаляйте их сами.

Автоматизация (пример, таймер **не установлен** этим набором):

```ini
# ~/.config/systemd/user/musicnx-backup.service
[Unit]
Description=MusicNX database backup
[Service]
Type=oneshot
Environment=MUSICNX_DB=/var/lib/musicnx/database/musicnx.db
Environment=MUSICNX_BACKUP_DIR=/var/lib/musicnx/backups
ExecStart=/bin/sh /opt/musicnx/scripts/backup.sh
```

```ini
# ~/.config/systemd/user/musicnx-backup.timer
[Unit]
Description=Daily MusicNX backup
[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now musicnx-backup.timer
systemctl --user list-timers musicnx-backup.timer
```

## 5. Восстановление

Живого restore API нет: БД заменяется только при остановленном сервисе.

```bash
# 1. посмотреть доступные снимки
ls -lt "$MUSICNX_BACKUP_DIR"/musicnx-*.db | head

# 2. восстановить (спросит подтверждение; сделает safety-копию)
sh scripts/restore.sh "$MUSICNX_BACKUP_DIR/musicnx-20261114-230000.db"

# неинтерактивно (скрипт/Ansible) — только осознанно
sh scripts/restore.sh --yes "$MUSICNX_BACKUP_DIR/musicnx-20261114-230000.db"

# dev/без systemd: сервис уже остановлен вручную
sh scripts/restore.sh --yes --no-service ./var/backups/musicnx-....db
```

Что делает скрипт: отклоняет относительные пути, не-`.db`, symlink, путь БД вне
существующего каталога и совпадение файла с БД; проверяет `PRAGMA quick_check`;
останавливает `systemctl --user stop musicnx`; делает `pre-restore-<ts>.db`
(при повреждённой БД — raw-копию с WAL/SHM); удаляет старые `-wal`/`-shm`;
атомарно подменяет БД (`mv` в том же каталоге), ставит `0600`; запускает сервис
и ждёт `200` от health (30 с). При сбое после остановки сервис поднимается
обратно.

Ручной fallback (если скрипт недоступен):

```bash
systemctl --user stop musicnx
sqlite3 /path/backup.db "PRAGMA quick_check;"        # ожидаем ok
cp -p /path/backup.db /path/musicnx.db
rm -f /path/musicnx.db-wal /path/musicnx.db-shm
chmod 600 /path/musicnx.db
systemctl --user start musicnx
curl -fsS http://127.0.0.1:8080/api/v1/system/health
```

## 6. Обновление

На Pi **нет Node.js**: frontend собирается на dev-машине (`make build`), на Pi
попадает готовый `backend/app/static`. `update.sh` требует его наличия и
откажется обновлять дерево без `app/static/index.html`.

Вариант A — git-checkout на Pi (APP_DIR — корень с `.git`):

```bash
cd /opt/musicnx/app
git status --porcelain          # должно быть пусто
sh /opt/musicnx/scripts/update.sh --branch main
```

Вариант B — prebuilt-релиз с dev-машины:

```bash
# dev: собрать и скопировать дерево backend/ (без .venv/var/.git) на Pi
make build
rsync -a --delete --exclude=.venv --exclude=var --exclude=.git \
  backend/ pi:/tmp/musicnx-release/

# Pi
MUSICNX_APP_DIR=/opt/musicnx/app MUSICNX_VENV=/opt/musicnx/venv \
MUSICNX_CONFIG=/opt/musicnx/config/config.yaml \
MUSICNX_DB=/var/lib/musicnx/database/musicnx.db \
  sh /opt/musicnx/scripts/update.sh --source /tmp/musicnx-release
```

Порядок: бэкап БД → снапшот кода (`<parent>/rollbacks/app-<ts>.tar.gz`) →
`systemctl --user stop` → обновление файлов (`git pull --ff-only` или
`rsync/cp`) → `pip install -r requirements.txt` → миграции (`app.database.migrations`
в cwd приложения и с `MUSICNX_CONFIG`) → старт → health. Любой сбой после
снапшота: остановка, восстановление файлов, повторная установка старых
зависимостей, старт, сообщение об ошибке. `--skip-deps` — если зависимости не
менялись; `--no-service` — dev.

Миграции **forward-only**: откат кода не откатывает схему. Если нужен даунгрейд
БД — `restore.sh` с бэкапом, сделанным перед обновлением (в выводе update.sh).

## 7. Soak-тест

Только GET-запросы; ничего не меняет. Пишет CSV и лог.

```bash
# 60 минут, опрос каждые 30 с
sh scripts/soak.sh --minutes 60

# быстрый smoke на 3 сэмпла
sh scripts/soak.sh --samples 3 --interval 5

# с явными путями
MUSICNX_SOAK_DIR=/var/lib/musicnx/soak \
  sh scripts/soak.sh --minutes 120 --interval 30
```

CSV: `timestamp,elapsed_s,health_http,health_status,health_db,uptime_s,
downloads_total,downloads_active,downloads_failed,sync_running,sync_last,
temp_c,throttled,rss_kb`. В лог пишется человекочитаемая строка на сэмпл и
итог: `health_ok/fail`, max temp, max RSS, число сэмплов с `throttled != 0x0`,
последний статус sync. Ctrl+C завершает и печатает итог.

`rss_kb` — RSS backend (`systemd MainPID`, fallback `pgrep -f uvicorn app.main:app`);
PID переопределяется на каждом сэмпле, если процесс сменился (деплой/рестарт),
поэтому длинный soak не деградирует в `na` после первого рестарта. Можно задать
`--pid`. `temp_c`/`throttled` — `vcgencmd`; если команда недоступна — `na`
(температура также читается из sysfs). Код выхода 0 — health хотя бы раз
ответил `200/ok`; 1 — health ни разу; 130 — прерывание.

**Длительный прогон 24 ч на Pi не выполнен** (см. §11 и [P8_NOTES.md](P8_NOTES.md));
soak-скрипт — инструмент для него, а не доказательство.

## 8. Логи

```bash
journalctl --user -u musicnx -n 100
journalctl --user -u musicnx --since "1 hour ago" --no-pager
journalctl --user -u musicnx --since "24 hours ago" | grep -iE 'error|critical|traceback'
journalctl --user -u musicnx -f
```

Файловый лог (если задан `logging.file`): `var/log/musicnx.log` с ротацией по
`logging.max_bytes`/`backup_count`. Redaction-фильтр скрывает токены/PIN
(docs/SECURITY.md). Логи MusicNX — только backend; mpv пишет в тот же
journal (дочерний процесс сервиса).

## 8.1 Метрики состояния и питание

Страница UI `/system/state` читает `GET /api/v1/system/metrics`: backend
сэмплит `/proc/stat`, `/proc/meminfo`, `/proc/uptime`, sysfs-температуру и
частоту, `vcgencmd get_throttled` и свободное место раз в 5 секунд. История —
кольцевой буфер в памяти (до 720 точек, ~1 час), в БД ничего не пишется; после
рестарта сервиса график начинается заново.

Кнопки «Перезагрузить»/«Выключить» вызывают `POST /system/reboot|shutdown` с
`{"confirm": true}`. Backend проверяет passwordless sudo (`sudo -n true`),
планирует `systemctl reboot|poweroff` через 2 секунды и сразу отвечает `202`.
Если у пользователя сервиса нет passwordless sudo (`NOPASSWD`),
эндпоинт вернёт `503`, а UI покажет ошибку. Не запускайте эти кнопки без
подтверждения: это останавливает воспроизведение и рвёт SSH/веб-сессию.

## 9. Rollback

- `update.sh` делает это автоматически при сбое обновления (файлы + сервис).  Снапшоты хранятся в `<parent APP_DIR>/rollbacks`, оставляются 3 последних.
- Ручной откат кода:

  ```bash
  systemctl --user stop musicnx
  tar -C /opt/musicnx/app -xzf /opt/musicnx/rollbacks/app-20261114-230000.tar.gz
  systemctl --user start musicnx
  curl -fsS http://127.0.0.1:8080/api/v1/system/health
  ```

  Снапшот не содержит `.git`, `.venv`, `var`; venv не откатывается (при
  необходимости поставьте прежние версии из `requirements.txt`).
- Откат БД — только `restore.sh` (см. §5); update.sh кладёт бэкап перед каждым
  обновлением.

## 10. Wi-Fi и AP-onboarding

В UI есть страница «Сеть» (`/network/status|scan|connect|disconnect`).
AP `MusicNX-Setup` — ручной helper (не автозапуск):

```bash
# поднять временную точку доступа (пароль генерируется, если не задан)
scripts/ap-onboarding.sh start --ifname wlan0
# телефон: подключиться к MusicNX-Setup → http://10.42.0.1:8080
scripts/ap-onboarding.sh stop
```

Автозапуск при отсутствии сети: `musicnx-ap-fallback.service` (enabled) вызывает
`ap-fallback.sh`; при eth0/Wi-Fi он ничего не делает. `ap-onboarding.sh` меняет Wi-Fi-интерфейс и профиль NetworkManager; не запускать
на интерфейсе, через который идёт management. AP не раздаёт интернет (нет NAT).
Обычное управление:

```bash
nmcli device wifi list
nmcli device wifi connect "<SSID>" password "<password>"
nmcli connection show --active
ip -br a; ip route
# приоритет Ethernet над Wi-Fi (пример)
nmcli connection modify "<eth>" ipv4.route-metric 100
nmcli connection modify "<wifi>" ipv4.route-metric 600
```

## 11. Честные ограничения

1. **Нет live restore API** — восстановление только остановленной БД через CLI.
2. **Нет OTA** — обновление вручную (git/source + update.sh), не P9.
3. **AP-onboarding — только helper** — `ap-onboarding.sh`, не автоматический
   режим и не NAT-роутер.
4. **Нет TLS** — трафик в LAN открыт; PIN удалён, мутации не авторизуются.
5. **OTA нет** — обновление через `update.sh`/release, без auto-update.
   TLS: LAN-only, при необходимости reverse proxy (Caddy/nginx) с сертификатом;
   встроенного TLS нет.
5. **Нет install.sh** в этом наборе — установка по DEPLOYMENT.md вручную.
6. **Бэкапы не офсайт** — `--copy-to` только копирует; NAS/rsync настраивает
   владелец.
7. **Soak 24 ч не проведён**; на тестовом Pi исторически under-voltage
   (`get_throttled` ≠ `0x0`), power gate снят владельцем (ADR-030), риск принят.
8. **Скрипты рассчитаны на systemd user service**; system-level/root не
   поддерживаются.
9. Скрипты проверены синтаксически и поведенчески на dev (WSL); реальный прогон
   на Pi с `systemctl --user`/`vcgencmd` — за владельцем.

## 12. Диагностика

| Проблема | Действие |
|---|---|
| backup: sqlite3 CLI is required | `sudo apt install sqlite3` |
| backup: integrity_check failed | не удалять снимок; проверить диск/БД, запустить `PRAGMA integrity_check` на живой БД |
| restore: cannot reach the systemd user bus | запускать от владельца сервиса, не через `sudo`; или `--no-service` на остановленной БД |
| restore: quick_check failed | снимок повреждён/не SQLite; взять другой `musicnx-*.db` |
| update: prebuilt app/static/index.html is missing | собрать frontend на dev (`make build`) и пересобрать релиз |
| update: pip install failed | сеть/PyPI/piwheels; зависимости не менялись — `--skip-deps`, затем повторить |
| update: health check failed | `journalctl --user -u musicnx -n 100`; код откатан, БД-бэкап на месте |
| soak: health not reachable | сервис не запущен/другой порт (`MUSICNX_BASE_URL`) |
| soak: temp/throttled = na | `vcgencmd` недоступен (нет Pi) или пользователь не в `video` group |
| БД заблокирована | не запущены ли два процесса; `busy_timeout` |
| BT: `br-connection-profile-unavailable` / `Protocol not available` | headless-сессия + WirePlumber seat monitoring; запустить `sh scripts/fix-bluetooth-headless.sh` и переподключить колонку |
| BT: `br-connection-key-missing` | устаревший bond: `bluetoothctl remove <MAC>`, pairing на колонке, заново `pair`/`trust`/`connect` |
| всё медленно, треск, обрывы BT/сети | `vcgencmd get_throttled` ≠ `0x0` и `dmesg | grep -i undervoltage`: заменить БП/кабель (5 В ≥2,5 А) |
| Яндекс-трек: 503 `cdn_timeout` / `cdn_unreachable` | `CdnRouter` не нашёл рабочий edge; `grep "CDN edge" var/log/musicnx.log`, проверить провайдера/VPN |
| очередь остановилась с `queue_exhausted_offline` | дальше только онлайн-треки, CDN помечен недоступным на 5 минут; проверить сеть, подождать или перезапустить сервис |
| страница не скроллится после караоке/очереди | обновить фронтенд: блокировка теперь один владелец (`html.scroll-locked`) и восстанавливает позицию; старый inline `body.overflow` снимается при загрузке |

