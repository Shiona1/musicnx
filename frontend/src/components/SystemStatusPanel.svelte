<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import Sparkline from './ui/Sparkline.svelte';
  import { api, ApiError } from '../lib/api';
  import type { RuntimeInfo, SystemMetrics, SystemShare } from '../lib/types';

  export let onToast: (message: string, tone?: 'info' | 'error') => void = () => {};

  const POLL_MS = 5_000;
  const WINDOW_MINUTES = 60;

  let metrics: SystemMetrics | null = null;
  let runtime: RuntimeInfo | null = null;
  let share: SystemShare | null = null;
  let error = '';
  let loading = true;
  let powerAction: 'reboot' | 'shutdown' | null = null;
  let powerBusy = false;
  let powerMessage = '';
  let timer: ReturnType<typeof setInterval> | null = null;

  $: samples = metrics?.samples ?? [];
  $: current = samples.length > 0 ? samples[samples.length - 1] : null;
  $: cpuValues = samples.map((sample) => sample.cpu_pct);
  $: memValues = samples.map((sample) => sample.mem_pct);
  $: tempValues = samples.map((sample) => sample.temp_c);
  $: throttle = current?.throttled ? parseThrottle(current.throttled) : null;

  onMount(() => {
    void refresh();
    void loadShare();
    timer = setInterval(() => void refresh(), POLL_MS);
    return () => stopPolling();
  });

  onDestroy(() => stopPolling());

  function stopPolling(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function refresh(): Promise<void> {
    if (powerMessage) return;
    try {
      const [next, info] = await Promise.all([
        api.systemMetrics(WINDOW_MINUTES),
        api.systemRuntime(),
      ]);
      metrics = next;
      runtime = info;
      error = '';
    } catch (cause) {
      error =
        cause instanceof ApiError
          ? cause.message
          : 'Не удалось получить метрики состояния.';
    } finally {
      loading = false;
    }
  }

  // One-shot: the share card is not part of the metrics polling.
  async function loadShare(): Promise<void> {
    try {
      share = await api.systemShare();
    } catch {
      share = null; // the card simply stays hidden
    }
  }

  function parseThrottle(raw: string): { value: number; warnings: string[] } {
    const value = Number.parseInt(raw.replace(/^0x/i, ''), 16);
    if (!Number.isFinite(value)) return { value: 0, warnings: [] };
    const warnings: string[] = [];
    if (value & 0x1) warnings.push('under-voltage сейчас');
    if (value & 0x2) warnings.push('частота ограничена');
    if (value & 0x4) warnings.push('throttling сейчас');
    if (value & 0x8) warnings.push('температурное ограничение');
    if (value & 0x10000) warnings.push('under-voltage фиксировался');
    if (value & 0x20000) warnings.push('частота ограничивалась');
    if (value & 0x40000) warnings.push('throttling фиксировался');
    if (value & 0x80000) warnings.push('температурное ограничение фиксировалось');
    return { value, warnings };
  }

  function formatBytes(value: number | null): string {
    if (value === null || !Number.isFinite(value)) return '—';
    const gib = value / 1024 ** 3;
    if (gib >= 1) return `${gib.toFixed(1)} ГБ`;
    return `${Math.round(value / 1024 ** 2)} МБ`;
  }

  function formatKb(value: number | null): string {
    if (value === null || !Number.isFinite(value)) return '—';
    return `${(value / 1024 / 1024).toFixed(2)} ГБ`;
  }

  function formatUptime(seconds: number | null): string {
    if (seconds === null || !Number.isFinite(seconds)) return '—';
    const total = Math.round(seconds);
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days > 0) return `${days} д ${hours} ч`;
    if (hours > 0) return `${hours} ч ${minutes} мин`;
    return `${minutes} мин`;
  }

  function numberText(value: number | null, unit = '', digits = 1): string {
    if (value === null || !Number.isFinite(value)) return '—';
    return `${value.toFixed(digits)}${unit}`;
  }

  function startPower(action: 'reboot' | 'shutdown'): void {
    powerAction = action;
  }

  async function confirmPower(): Promise<void> {
    if (!powerAction) return;
    powerBusy = true;
    try {
      if (powerAction === 'reboot') await api.reboot();
      else await api.shutdown();
      stopPolling();
      powerMessage =
        powerAction === 'reboot'
          ? 'Raspberry Pi перезагружается. Через ~30 секунд обновите страницу.'
          : 'Raspberry Pi выключается. Отключите питание после остановки системы.';
      onToast(powerMessage);
      powerAction = null;
    } catch (cause) {
      onToast(
        cause instanceof ApiError
          ? cause.message
          : 'Не удалось выполнить действие с питанием.',
        'error',
      );
    } finally {
      powerBusy = false;
    }
  }

  function reloadPage(): void {
    window.location.reload();
  }
</script>

<section class="state material-content" aria-label="Состояние системы">
  <header class="state-head">
    <div>
      <span class="eyebrow">Система</span>
      <h1>Состояние</h1>
      <p class="sub">
        Температура, процессор, память и питание Pi за последний час. Данные
        обновляются каждые 5 секунд.
      </p>
    </div>
    <div class="head-actions">
      {#if current}
        <span class="chip" class:warning={throttle && throttle.value !== 0}>
          <Icon name="activity" size={13} />
          {throttle && throttle.value !== 0 ? 'питание проседает' : 'питание в норме'}
        </span>
      {/if}
      <button type="button" class="ghost" onclick={() => void refresh()} disabled={loading}>
        Обновить
      </button>
    </div>
  </header>

  {#if error}
    <div class="notice error" role="alert">
      <strong>Метрики недоступны.</strong>
      <span>{error}</span>
    </div>
  {/if}

  {#if share}
    <article class="card share-card" aria-label="Открыть на телефоне">
      <div class="share-copy">
        <span class="metric-label"><Icon name="monitor" size={14} /> Открыть на телефоне</span>
        <strong class="share-url">{share.url}</strong>
        {#if share.loopback}
          <span class="metric-hint">
            Этот адрес — локальный: с телефона он не откроется. Откройте интерфейс
            по LAN-адресу — и QR станет рабочим.
          </span>
        {:else}
          <span class="metric-hint">Наведите камеру телефона — интерфейс откроется в его браузере.</span>
        {/if}
      </div>
      {#if share.qr_data_uri}
        <img
          class="share-qr"
          src={share.qr_data_uri}
          width="132"
          height="132"
          alt={`QR-код со ссылкой ${share.url}`}
        />
      {:else}
        <span class="metric-hint">QR не отрисовался — ссылку можно ввести вручную.</span>
      {/if}
    </article>
  {/if}

  {#if powerMessage}
    <div class="notice info" role="status">
      <Icon name="power" size={16} />
      <span>{powerMessage}</span>
      {#if powerAction === null}
        <button type="button" class="ghost small" onclick={reloadPage}>Обновить страницу</button>
      {/if}
    </div>
  {/if}

  {#if loading && samples.length === 0}
    <div class="cards" aria-hidden="true">
      {#each [0, 1, 2, 3] as key (key)}
        <div class="card skeleton"></div>
      {/each}
    </div>
  {:else if samples.length === 0}
    <div class="empty">
      <Icon name="activity" size={26} />
      <div>
        <strong>Пока нет ни одного сэмпла</strong>
        <p>Сервер начнёт собирать метрики при старте; подождите несколько секунд.</p>
      </div>
    </div>
  {:else}
    <div class="cards">
      <article class="card metric">
        <span class="metric-label"><Icon name="thermometer" size={14} /> Температура</span>
        <strong class="metric-value">{numberText(current?.temp_c ?? null, ' °C')}</strong>
        <span class="metric-hint">max {numberText(Math.max(...tempValues.filter((v): v is number => v !== null)), ' °C')}</span>
        <Sparkline values={tempValues} min={30} max={85} stroke="var(--danger)" height={54} />
      </article>

      <article class="card metric">
        <span class="metric-label"><Icon name="cpu" size={14} /> CPU</span>
        <strong class="metric-value">{numberText(current?.cpu_pct ?? null, ' %')}</strong>
        <span class="metric-hint">
          {current?.clock_mhz ? `${current.clock_mhz} МГц` : 'частота —'} · load {numberText(current?.load1 ?? null, '', 2)}
        </span>
        <Sparkline values={cpuValues} min={0} max={100} height={54} />
      </article>

      <article class="card metric">
        <span class="metric-label"><Icon name="memory" size={14} /> Память</span>
        <strong class="metric-value">{numberText(current?.mem_pct ?? null, ' %')}</strong>
        <span class="metric-hint">
          {formatKb(current?.mem_used_kb ?? null)} из {formatKb(current?.mem_total_kb ?? null)}
        </span>
        <Sparkline values={memValues} min={0} max={100} stroke="var(--accent)" height={54} />
      </article>

      <article class="card metric">
        <span class="metric-label"><Icon name="disk" size={14} /> Диск</span>
        <strong class="metric-value">{formatBytes(current?.disk_free_bytes ?? null)}</strong>
        <span class="metric-hint">свободно</span>
        <span class="metric-hint">uptime {formatUptime(current?.uptime_s ?? null)}</span>
      </article>
    </div>

    {#if runtime}
      <div class="cards runtime-cards">
        <article class="card metric">
          <span class="metric-label"><Icon name="queue" size={14} /> Очередь / WS</span>
          <strong class="metric-value">{runtime.queue_depth}</strong>
          <span class="metric-hint">WS клиентов: {runtime.ws_clients}</span>
        </article>
        <article class="card metric">
          <span class="metric-label"><Icon name="download" size={14} /> Загрузки</span>
          <strong class="metric-value">{runtime.downloads.total}</strong>
          <span class="metric-hint">активных: {runtime.downloads.by_status.downloading ?? 0}</span>
        </article>
        <article class="card metric">
          <span class="metric-label"><Icon name="activity" size={14} /> mpv</span>
          <strong class="metric-value">{runtime.audio.ready ? 'готов' : 'нет связи'}</strong>
          <span class="metric-hint">restarts: {runtime.audio.restarts ?? '—'}</span>
        </article>
        <article class="card metric">
          <span class="metric-label"><Icon name="disk" size={14} /> БД</span>
          <strong class="metric-value">{formatBytes(runtime.database.size_bytes)}</strong>
          <span class="metric-hint">uptime {formatUptime(runtime.uptime_s)}</span>
        </article>
      </div>
    {/if}

    <div class="power-grid">
      <article class="card power-card">
        <div class="power-copy">
          <span class="metric-label"><Icon name="activity" size={14} /> Питание</span>
          <strong>{throttle && throttle.value !== 0 ? 'Есть предупреждения' : 'Норма'}</strong>
          <span class="metric-hint">raw: {current?.throttled ?? '—'}</span>
          {#if throttle && throttle.warnings.length > 0}
            <ul class="warnings">
              {#each throttle.warnings as warning (warning)}
                <li>{warning}</li>
              {/each}
            </ul>
          {:else}
            <p class="metric-hint">
              Under-voltage приводит к рывкам, треску и обрывам Bluetooth. Нужен
              БП 5 В ≥2,5 А.
            </p>
          {/if}
        </div>
      </article>

      <article class="card power-card">
        <div class="power-copy">
          <span class="metric-label"><Icon name="power" size={14} /> Управление питанием</span>
          <strong>Перезагрузка и выключение</strong>
          <p class="metric-hint">
            Сервер поставит действие в очередь и ответит до отключения. После
            перезагрузки страница станет доступна через ~30 секунд.
          </p>
          <div class="power-actions">
            <button
              type="button"
              class="ghost restart"
              disabled={powerBusy || powerMessage !== ''}
              onclick={() => startPower('reboot')}
            >
              <Icon name="restart" size={15} /> Перезагрузить
            </button>
            <button
              type="button"
              class="ghost danger"
              disabled={powerBusy || powerMessage !== ''}
              onclick={() => startPower('shutdown')}
            >
              <Icon name="power" size={15} /> Выключить
            </button>
          </div>
        </div>
      </article>
    </div>
  {/if}
</section>

{#if powerAction}
  <div class="modal-backdrop material-scrim" role="presentation">
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="power-title">
      <h2 id="power-title">
        {powerAction === 'reboot' ? 'Перезагрузить Raspberry Pi?' : 'Выключить Raspberry Pi?'}
      </h2>
      <p>
        {powerAction === 'reboot'
          ? 'Текущее воспроизведение остановится, сервер уйдёт в перезагрузку. Страница перестанет отвечать на ~30 секунд.'
          : 'Сервер выключится. Включить его можно будет только физически, кнопкой питания.'}
      </p>
      <div class="modal-actions">
        <button type="button" class="ghost" disabled={powerBusy} onclick={() => (powerAction = null)}>
          Отмена
        </button>
        <button
          type="button"
          class="primary"
          class:danger={powerAction === 'shutdown'}
          disabled={powerBusy}
          onclick={confirmPower}
        >
          {powerBusy ? 'Отправляем…' : powerAction === 'reboot' ? 'Перезагрузить' : 'Выключить'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .state {
    padding: clamp(18px, 2.2vw, 28px);
    border-radius: 26px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }

  .state-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }

  .eyebrow {
    color: var(--accent-2);
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  h1 {
    margin: 2px 0 4px;
    font-size: clamp(1.5rem, 2.6vw, 2rem);
    letter-spacing: -0.015em;
  }

  .sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    max-width: 78ch;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid rgba(34, 211, 238, 0.28);
    background: rgba(34, 211, 238, 0.08);
    color: #a5f3fc;
    font-size: 0.7rem;
    font-weight: 700;
  }

  .chip.warning {
    border-color: rgba(248, 113, 113, 0.35);
    background: rgba(248, 113, 113, 0.1);
    color: #fecdd3;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 14px;
    border-radius: 18px;
    border: 1px solid var(--border);
    background: linear-gradient(160deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.02));
    min-width: 0;
  }

  .card.skeleton {
    min-height: 130px;
    background: linear-gradient(100deg, rgba(255, 255, 255, 0.04) 25%, rgba(255, 255, 255, 0.09) 45%, rgba(255, 255, 255, 0.04) 65%);
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .share-card {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
  }

  .share-copy {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .share-url {
    color: var(--text);
    font-size: 1rem;
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  .share-qr {
    flex: none;
    width: 132px;
    height: 132px;
    padding: 8px;
    border-radius: 12px;
    background: #ffffff;
  }

  @media (max-width: 560px) {
    .share-card {
      flex-direction: column;
      align-items: stretch;
    }
    .share-qr {
      align-self: center;
    }
  }

  .metric-label {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.02em;
  }

  .metric-value {
    color: var(--text);
    font-size: 1.45rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }

  .metric-hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.68rem;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .power-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }

  .power-card {
    justify-content: space-between;
    gap: 10px;
  }

  .power-copy {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .power-copy strong {
    color: var(--text);
    font-size: 0.95rem;
  }

  .warnings {
    margin: 2px 0 0;
    padding-left: 18px;
    color: #fecdd3;
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .power-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 4px;
  }

  .ghost {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 12px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.74rem;
    font-weight: 700;
  }

  .ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .ghost.small {
    padding: 5px 10px;
    font-size: 0.68rem;
  }

  .ghost.restart {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
  }

  .ghost.danger {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.35);
    background: rgba(248, 113, 113, 0.08);
  }

  .ghost.danger:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.16);
  }

  .notice {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 11px 14px;
    border-radius: 14px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.78rem;
  }

  .notice.info {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.28);
    background: rgba(34, 211, 238, 0.07);
  }

  .notice.error {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
  }

  .notice span {
    flex: 1 1 220px;
    overflow-wrap: anywhere;
  }

  .empty {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 22px;
    border-radius: 18px;
    border: 1px dashed var(--border);
    color: var(--muted);
  }

  .empty strong {
    color: var(--text);
    display: block;
    margin-bottom: 3px;
  }

  .empty p {
    margin: 0;
    font-size: 0.78rem;
  }

  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: grid;
    place-items: center;
    padding: 20px;
    background: rgba(4, 6, 12, 0.68);
  }

  .modal {
    width: min(420px, 100%);
    padding: 20px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: #0d1220;
    box-shadow: 0 30px 70px -24px rgba(0, 0, 0, 0.9);
  }

  .modal h2 {
    margin: 0 0 8px;
    font-size: 1.05rem;
  }

  .modal p {
    margin: 0 0 14px;
    color: var(--muted);
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }

  .primary {
    padding: 8px 16px;
    border-radius: 999px;
    border: 0;
    background: var(--accent);
    color: #0b0715;
    font-size: 0.76rem;
    font-weight: 700;
  }

  .primary.danger {
    background: var(--danger);
    color: #180608;
  }

  @keyframes shimmer {
    0% { background-position: 120% 0; }
    100% { background-position: -40% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .card.skeleton { animation: none; }
  }

  @media (max-width: 560px) {
    .cards { grid-template-columns: 1fr 1fr; }
    .power-actions .ghost { flex: 1 1 auto; justify-content: center; }
  }
</style>
