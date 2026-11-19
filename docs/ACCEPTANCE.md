# SPEC §14 — acceptance checklist (v1)

Дата: 2026-11-15. Статусы: **done** — подтверждено кодом/тестом/реальным
смоуком; **pending** — требует оборудования/времени владельца; **blocked** —
внешнее ограничение, не дефект кода. Секреты/PIN не приводятся.

| # | Критерий SPEC §14 | Статус | Доказательство / что осталось |
|---|---|---|---|
| 1 | Pi запускает MusicNX без рабочего стола и браузера | done | systemd user service `musicnx` active; mpv headless; UI с другого устройства |
| 2 | Web UI доступен с другого устройства в LAN | done | `http://<pi-address>:8080`, Host/Origin проверки |
| 3 | Импорт локальных файлов, поиск, очередь, управление | done | P2 tests; локальная библиотека/поиск/очередь; реальный UI |
| 4 | mpv воспроизводит локальные файлы | done | real mpv playback + `/stream` local 206; Pi Test Tone удалён как seed |
| 5 | Состояние восстанавливается после перезагрузки | done | restore после reboot проверен в P1/P3; queue/player state в SQLite |
| 6 | Device Flow работает; токены обновляются | done | real Device Flow + forced refresh; token 0600 |
| 7 | Онлайн-трек Яндекса воспроизводится | done | real track 8 playing, `/stream` 206, mpv |
| 8 | Моя волна запускается, feedback влияет | done (real start + mock feedback) | real wave start 5 tracks; like/dislike/skip API/UI tests |
| 9 | Трек/альбом/плейлист скачивается, файл проверен, offline честный | done (track + playlist real; full album soak pending) | real `flac-mp4` → `.m4a` 29 745 710 B; playlist `Sub33` → MP3 1.2 MB; verify/offline; full album/playlist soak pending |
| 10 | Удаление из внешнего плейлиста не удаляет локальный файл | done | sync composition replace + test; media_files untouched |
| 11 | Повторная синхронизация без дублей; раз в час | partial | idempotent rerun test 0/0/0; **hourly real soak pending** (manual real sync success 668 tracks) |
| 12 | Ошибка сети не повреждает библиотеку | done | P6 partial/failed tests; no destructive deletes; retry/backoff |
| 13 | UI показывает доступность каждой композиции | done | availability badge on all shelves/rows |
| 14 | Тексты/LRC, караоке с offset зоны | partial | LRC/plain, karaoke, track offset, LRCLIB fallback done; **per-zone offset in lyrics pending** |
| 15 | Зоны jack/USB-DAC/BT/browser, переключение без прерывания | partial | jack + browser real; BT Fosi pair/connect/A2DP real; physical listening and USB-DAC pending |
| 16 | Звук в браузере: Range-перемотка | done | Range proxy 206 tests; BrowserAudio drift tolerance 500 ms resync |
| 17 | Длительная работа без утечки памяти/роста логов | pending | 24h soak **прерван** (3 сэмпла); нужен повтор после БП, план — docs/SOAK_24H_REPORT.md |
| 18 | Реальные RAM/CPU/температура на Pi 3 | done (short), pending 24h | 12 samples; CPU/RSS/temp/throttled CSV; long run pending |

## P8 close status

- PIN: **removed by owner** (docs/PIN_REMOVAL.md); LAN mutations open. Owner
  chose variant A (trusted LAN + accepted risk, 2026-11-16); this is residual
  risk, not an auth control; see ADR-EX-001.
- TLS: **not required for v1** (LAN); optional reverse-proxy notes in
  `docs/OPERATIONS.md`; ADR-035.
- AP onboarding: open AP tested on wlan0 as the service user via polkit:
  `MusicNX-Setup`, `10.42.0.1/24`, `/setup` → 200, then stopped. NM shared mode
  DHCP/NAT. WPA hotspot hit `key setting validation failed` on Pi 3 driver;
  **phone test pending owner**.
- 24h soak: **not accepted / interrupted**; статус и план — docs/SOAK_24H_REPORT.md.
  Повтор только после замены БП и `throttled=0x0`.
- AP fallback when no known network: `scripts/ap-fallback.sh` (manual/systemd
  root install; not auto-enabled to avoid cutting management).
