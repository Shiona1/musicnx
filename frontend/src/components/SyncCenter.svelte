<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount } from 'svelte';
  import {
    countsSummary,
    countsTotal,
    describeSyncError,
    formatClockTime,
    formatIntervalSeconds,
    formatRelativeTime,
    formatRunDuration,
    formatSyncDate,
    formatSyncElapsed,
    isRetryableSyncPinError,
    latestSuccessAt,
    progressPercent,
    progressText,
    syncPhaseLabel,
    syncStatusLabel,
    syncStatusTone,
    syncTriggerLabel,
    SyncPinCancelledError,
    withSyncPinRetry,
  } from '../lib/sync';
  import type { SyncCounts, SyncPinPrompt, SyncProgress, SyncRun } from '../lib/sync';
  import {
    refreshSync,
    requestSyncCancel,
    requestSyncStart,
    startSyncPolling,
    syncError,
    syncHistory,
    syncHistoryTotal,
    syncLoading,
    syncProgress,
    syncRouteMissing,
    syncStatus,
    syncUpdatedAt,
  } from '../lib/syncStores';

  /**
   * P6 Sync Center — status of the server-side sync engine (docs/API.md §10).
   *
   * Every value on screen is server-owned: `GET /sync/status` supplies the last
   * run, the schedule and the live progress, `GET /sync/history?limit=20` the
   * newest runs. Start/cancel are PIN/localhost protected mutations: the PIN is
   * requested by the built-in dialog (or the injected `onPinRequired` hook),
   * sent to `POST /auth/pin` and the exact request is replayed — the PIN is
   * never stored and the browser holds no Yandex token.
   *
   * A backend without P6 answers `404`: the view says so and offers a retry
   * instead of rendering a fake schedule (AGENTS.md §12). Counts and the error
   * are shown exactly as the server reported them; a missing field is rendered
   * as "сервер не прислал", never as a zero.
   */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: SyncPinPrompt | null = null;

  const MAX_PIN_ROUNDS = 3;
  /** Relative labels ("через 12 минут") are refreshed without a network call. */
  const RELATIVE_TICK_MS = 30_000;

  let busyAction: 'start' | 'cancel' | null = null;
  let actionMessage = '';
  let actionKind: 'ok' | 'error' = 'ok';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let now = Date.now();

  $: status = $syncStatus;
  $: running = status?.running === true;
  $: progress = $syncProgress;
  $: percent = progressPercent(progress);
  $: progressLabel = progressText(progress);
  $: phase = syncPhaseLabel(progress?.phase ?? null);
  $: history = $syncHistory;
  // `status.last` is the newest *finished* run; history may already contain the
  // active one, which belongs to the "Сейчас" card instead.
  $: lastRun = status?.last ?? history.find((run) => run.status !== 'running') ?? null;
  $: lastCounts = lastRun?.counts ?? status?.counts ?? null;
  $: playlistCounts = lastRun?.playlist_counts ?? null;
  $: lastAttemptAt =
    status?.last_attempt_at ?? lastRun?.finished_at ?? lastRun?.started_at ?? null;
  $: lastAttemptStatus = status?.last_attempt_status ?? lastRun?.status ?? null;
  $: lastError = status?.last_error ?? lastRun?.error ?? null;
  $: lastSuccessAt = latestSuccessAt(status, history);
  $: nextRunAt = status?.next_run_at ?? null;
  $: intervalLabel = formatIntervalSeconds(status?.interval_s ?? null);
  $: runningRun = history.find((run) => run.status === 'running') ?? null;
  $: runningSince = runningRun?.started_at ?? null;
  $: elapsed = running ? formatSyncElapsed(runningSince, now) : null;
  $: lastDuration = lastRun ? formatRunDuration(lastRun) : null;
  $: lastTrigger = lastRun ? syncTriggerLabel(lastRun.trigger) : null;
  $: noChanges = lastCounts !== null && countsTotal(lastCounts) === 0;
  $: historyHint =
    $syncHistoryTotal > history.length ? `показано ${history.length} из ${$syncHistoryTotal}` : '';
  $: empty =
    !$syncLoading && status === null && history.length === 0 && !$syncRouteMissing && !$syncError;

  onMount(() => {
    const stopPolling = startSyncPolling();
    const tick = window.setInterval(() => {
      now = Date.now();
    }, RELATIVE_TICK_MS);
    return () => {
      stopPolling();
      window.clearInterval(tick);
    };
  });

  onDestroy(() => {
    // A dialog unmounted mid-action must not leave the promise hanging.
    const resolve = pinResolve;
    pinResolve = null;
    if (resolve) resolve(null);
  });

  function notify(text: string, kind: 'ok' | 'error' = 'ok'): void {
    if (onToast) {
      actionMessage = '';
      onToast(text, kind === 'error' ? 'error' : 'info');
      return;
    }
    actionMessage = text;
    actionKind = kind;
  }

  function askPin(): Promise<string | null> {
    if (onPinRequired) return onPinRequired();
    return new Promise((resolve) => {
      const previous = pinResolve;
      pinResolve = null;
      if (previous) previous(null);
      pin = '';
      showPin = true;
      pinResolve = (value) => {
        pinResolve = null;
        showPin = false;
        resolve(value);
      };
    });
  }

  function submitPin(event: SubmitEvent): void {
    event.preventDefault();
    if (!pin.trim() || !pinResolve) return;
    const resolve = pinResolve;
    pinResolve = null;
    showPin = false;
    resolve(pin);
  }

  function cancelPin(): void {
    const resolve = pinResolve;
    pinResolve = null;
    showPin = false;
    pin = '';
    pinError = '';
    if (resolve) resolve(null);
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && showPin) cancelPin();
  }

  type Outcome = { ok: true } | { ok: false; message: string };

  /** Replays a mutation after PIN login; keeps the dialog open on a wrong PIN. */
  async function mutateWithPin(operation: () => Promise<void>): Promise<Outcome> {
    for (let round = 0; round < MAX_PIN_ROUNDS; round += 1) {
      try {
        await withSyncPinRetry(operation, askPin);
        return { ok: true };
      } catch (error) {
        if (error instanceof SyncPinCancelledError) {
          return { ok: false, message: 'Действие отменено: PIN не введён.' };
        }
        if (isRetryableSyncPinError(error) && !onPinRequired) {
          pinError = describeSyncError(error);
          continue;
        }
        return { ok: false, message: describeSyncError(error) };
      }
    }
    return { ok: false, message: 'PIN не принят: действие не выполнено.' };
  }

  async function runStart(): Promise<void> {
    if (busyAction !== null || running) return;
    busyAction = 'start';
    actionMessage = '';
    pinError = '';
    try {
      // The PIN layer lives in mutateWithPin only: the store rethrows
      // `auth_required` so the replay happens exactly once, after login.
      const outcome = await mutateWithPin(async () => {
        await requestSyncStart();
      });
      if (!outcome.ok) {
        notify(outcome.message, 'error');
        return;
      }
      notify('Синхронизация запущена.');
    } finally {
      busyAction = null;
    }
  }

  async function runCancel(): Promise<void> {
    if (busyAction !== null || !running) return;
    busyAction = 'cancel';
    actionMessage = '';
    pinError = '';
    try {
      const outcome = await mutateWithPin(async () => {
        await requestSyncCancel();
      });
      if (!outcome.ok) {
        notify(outcome.message, 'error');
        return;
      }
      notify('Отмена запрошена: сервер остановит прогон между плейлистами.');
    } finally {
      busyAction = null;
    }
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }

  function timeText(value: string | null | undefined): string {
    return formatSyncDate(value);
  }

  function relativeText(value: string | null | undefined): string | null {
    return formatRelativeTime(value ?? null, now);
  }

  function durationText(run: SyncRun | null): string | null {
    return run === null ? null : formatRunDuration(run);
  }

  function triggerText(run: SyncRun | null): string | null {
    return run === null ? null : syncTriggerLabel(run.trigger);
  }

  function finishClock(value: string | null): string | null {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function countsMini(counts: SyncCounts | null): string {
    if (!counts) return '—';
    return `+${counts.added} · ~${counts.updated} · −${counts.removed}`;
  }

  function progressDetail(current: SyncProgress | null): string | null {
    if (current === null) return null;
    const label = progressText(current);
    if (label === null) return null;
    return label;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section class="sync material-content" aria-labelledby="sync-title">
  <header class="sync-head">
    <div>
      <span class="eyebrow">Яндекс Музыка</span>
      <h1 id="sync-title">Синхронизация</h1>
      <p class="sub">
        Плейлисты и «Мне нравится» на сервере обновляет только backend — по
        расписанию или вручную. Здесь состояние прогона, счётчики изменений и
        история; токен Яндекса в браузер не попадает, локальные файлы при
        синхронизации не удаляются.
      </p>
    </div>
    <div class="head-actions">
      <span class="chip state" class:running>
        <span class="dot" aria-hidden="true"></span>
        {running ? 'Синхронизация идёт' : 'Не идёт'}
      </span>
      {#if intervalLabel}
        <span class="chip schedule" title="Интервал фоновой синхронизации">{intervalLabel}</span>
      {/if}
      {#if $syncUpdatedAt > 0}
        <span class="updated" title="Последнее успешное обновление по REST">
          обновлено {formatClockTime($syncUpdatedAt)}
        </span>
      {/if}
      <button
        type="button"
        class="ghost"
        disabled={$syncLoading}
        onclick={() => void refreshSync()}
      >
        Обновить
      </button>
    </div>
  </header>

  {#if actionMessage}
    <div
      class="notice"
      class:error={actionKind === 'error'}
      role={actionKind === 'error' ? 'alert' : 'status'}
    >
      {actionMessage}
    </div>
  {/if}

  {#if $syncRouteMissing}
    <div class="notice error" role="alert">
      <strong>Сервер не поддерживает синхронизацию.</strong>
      <span>
        <code>GET /api/v1/sync/status</code> вернул 404: в этой сборке backend
        нет маршрутов P6. Расписание здесь не выдумывается, кнопки запуска и
        отмены не сработают — включите P6 backend и повторите.
      </span>
      <button type="button" class="ghost small" onclick={() => void refreshSync()}>
        Повторить
      </button>
    </div>
  {:else if $syncError}
    <div class="notice error" role="alert">
      <span>{$syncError}</span>
      <button type="button" class="ghost small" onclick={() => void refreshSync()}>
        Повторить
      </button>
    </div>
  {/if}

  <span class="sr-only" role="status" aria-live="polite">
    {running
      ? `Синхронизация идёт${phase ? `: ${phase}` : ''}${progressLabel ? `, ${progressLabel}` : ''}`
      : 'Синхронизация не идёт'}
  </span>

  {#if !$syncRouteMissing && $syncLoading && status === null}
    <div class="cards" aria-hidden="true">
      {#each [0, 1, 2] as key (key)}
        <div class="card skeleton-card">
          <span class="skeleton-line short"></span>
          <span class="skeleton-line"></span>
          <span class="skeleton-line"></span>
        </div>
      {/each}
    </div>
    <span class="sr-only" role="status">Загружаем состояние синхронизации…</span>
  {:else if !$syncRouteMissing}
    {#if empty}
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor" aria-hidden="true">
          <path
            d="M12 4V1L8 5l4 4V6a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8zm-1 5h2v4h4v2h-6z"
          />
        </svg>
        <div>
          <strong>Синхронизаций ещё не было</strong>
          <p>
            История пуста, и сервер не сообщил расписание. Запустите
            синхронизацию вручную — результат со счётчиками появится здесь, а
            расписание (если включено в конфиге) — после первого прогона.
          </p>
        </div>
      </div>
    {:else}
      <div class="cards">
        <article class="card now" class:active={running} aria-labelledby="sync-now-title">
          <div class="card-head">
            <h2 id="sync-now-title">Сейчас</h2>
            <span class="status {running ? 'running' : 'idle'}">
              {running ? 'Идёт' : 'Ожидание'}
            </span>
          </div>

          {#if running}
            {#if phase !== null || progressDetail(progress) !== null}
              <p class="phase-line">
                {#if phase !== null}<span class="phase">{phase}</span>{/if}
                {#if progressDetail(progress) !== null}
                  <span class="phase-count">{progressDetail(progress)}</span>
                {/if}
              </p>
            {:else}
              <p class="idle-line">
                Прогон идёт, но сервер не прислал подробный прогресс
                (phase/current/total).
              </p>
            {/if}

            {#if percent !== null}
              <span
                class="bar"
                role="progressbar"
                aria-valuemin="0"
                aria-valuemax="100"
                aria-valuenow={percent}
                aria-valuetext={`${progressDetail(progress) ?? 'прогресс'} · ${percent}%`}
                aria-label="Прогресс синхронизации"
              >
                <span style={`width: ${percent}%`}></span>
              </span>
            {:else}
              <span class="bar indeterminate" aria-hidden="true"><span></span></span>
            {/if}

            <p class="meta">
              {#if runningSince}
                <span>с {timeText(runningSince)}</span>
              {/if}
              {#if elapsed}
                <span>· {elapsed}</span>
              {/if}
              {#if percent !== null}
                <span>· {percent}%</span>
              {/if}
            </p>
            <p class="hint">
              Прогресс уточняется событиями WS <code>sync.progress</code>, пока
              прогон активен, статус перечитывается каждые 2 секунды.
            </p>
          {:else}
            <p class="idle-line">Синхронизация не идёт.</p>
            <p class="hint">
              Ручной запуск обновит плейлисты и «Мне нравится» на сервере.
              Локальные файлы и офлайн-доступ не затрагиваются.
            </p>
          {/if}
        </article>

        <article class="card" aria-labelledby="sync-last-title">
          <div class="card-head">
            <h2 id="sync-last-title">Последняя синхронизация</h2>
            {#if lastAttemptStatus !== null}
              <span class="status {syncStatusTone(lastAttemptStatus)}">
                {syncStatusLabel(lastAttemptStatus)}
              </span>
            {/if}
          </div>

          {#if lastRun === null && lastAttemptAt === null}
            <p class="idle-line">Синхронизаций ещё не было.</p>
            <p class="hint">
              После первого прогона здесь появятся статус, счётчики
              добавленных/обновлённых/удалённых записей и текст ошибки.
            </p>
          {:else}
            <p class="meta">
              {#if lastAttemptAt}
                <span>попытка: {timeText(lastAttemptAt)}</span>
              {/if}
              {#if lastTrigger}
                <span>· {lastTrigger}</span>
              {/if}
              {#if lastDuration}
                <span>· длилась {lastDuration}</span>
              {/if}
            </p>

            {#if lastCounts !== null}
              <div class="stats" role="group" aria-label="Изменения за последнюю синхронизацию">
                <div class="stat added">
                  <span class="value">{lastCounts.added}</span>
                  <span class="label">добавлено</span>
                </div>
                <div class="stat updated">
                  <span class="value">{lastCounts.updated}</span>
                  <span class="label">обновлено</span>
                </div>
                <div class="stat removed">
                  <span class="value">{lastCounts.removed}</span>
                  <span class="label">удалено</span>
                </div>
              </div>
              {#if noChanges}
                <p class="hint">
                  Изменений нет: состав совпал, повторный прогон идемпотентен.
                </p>
              {/if}
              {#if playlistCounts !== null}
                <p class="hint">Плейлисты: {countsSummary(playlistCounts)}.</p>
              {/if}
            {:else}
              <p class="hint">
                Сервер не прислал счётчики added/updated/removed для этого
                прогона.
              </p>
            {/if}

            {#if lastError}
              <p class="error-line" role="note">{lastError}</p>
            {/if}

            <p class="hint">
              Последний успех: {#if lastSuccessAt}
                {timeText(lastSuccessAt)}{relativeText(lastSuccessAt)
                  ? ` (${relativeText(lastSuccessAt)})`
                  : ''}
              {:else}
                успешных прогонов ещё не было
              {/if}.
            </p>          {/if}
        </article>

        <article class="card" aria-labelledby="sync-next-title">
          <div class="card-head">
            <h2 id="sync-next-title">Следующая синхронизация</h2>
            {#if intervalLabel}
              <span class="status idle">{intervalLabel}</span>
            {/if}
          </div>

          {#if nextRunAt !== null}
            <p class="big">{timeText(nextRunAt)}</p>
            <p class="meta">
              {#if relativeText(nextRunAt)}<span>{relativeText(nextRunAt)}</span>{/if}
              {#if intervalLabel}<span>· {intervalLabel}</span>{/if}
            </p>
            <p class="hint">
              Расписание живёт на сервере: клиент только показывает ближайший
              запуск и не запускает таймеров сам.
            </p>
          {:else if intervalLabel !== null}
            <p class="idle-line">Ближайший запуск не назначен.</p>
            <p class="hint">
              Интервал по конфигу: {intervalLabel}. Сервер не прислал
              <code>next_run_at</code>: возможно, фоновая синхронизация выключена
              (<code>sync.enabled=false</code>) или расписание ещё не
              спланировано. Ручной запуск доступен.
            </p>
          {:else}
            <p class="idle-line">Расписание не задано.</p>
            <p class="hint">
              Сервер не прислал <code>next_run_at</code>/<code>interval_s</code>:
              вероятно, фоновая синхронизация выключена
              (<code>sync.enabled=false</code>). Ручной запуск доступен.
            </p>
          {/if}
        </article>
      </div>

      <div class="actions">
        <button
          type="button"
          class="primary"
          disabled={running || busyAction !== null || $syncRouteMissing}
          title={running
            ? 'Синхронизация уже идёт — дождитесь завершения или отмените её'
            : 'Запустить синхронизацию сейчас'}
          aria-label="Запустить синхронизацию сейчас"
          onclick={() => void runStart()}
        >
          {busyAction === 'start' ? 'Запускаем…' : 'Синхронизировать сейчас'}
        </button>
        <button
          type="button"
          class="ghost danger"
          disabled={!running || busyAction !== null || $syncRouteMissing}
          title={running
            ? 'Отменить прогон между плейлистами'
            : 'Синхронизация не идёт — отменять нечего'}
          aria-label="Отменить синхронизацию"
          onclick={() => void runCancel()}
        >
          {busyAction === 'cancel' ? 'Отменяем…' : 'Отменить'}
        </button>
        <p class="hint actions-hint">
          Запуск и отмена — изменяющие действия. Отмена срабатывает между
          плейлистами: текущая транзакция завершается, затем прогон
          останавливается.
        </p>
      </div>

      <div class="group">
        <div class="group-head">
          <h2>История</h2>
          <span class="group-count">
            {history.length}{#if $syncHistoryTotal > history.length} из {$syncHistoryTotal}{/if}
          </span>
          {#if historyHint}
            <span class="hint-inline">{historyHint}</span>
          {/if}
        </div>

        {#if history.length === 0}
          <p class="group-empty">
            История пуста. Сервер хранит до 20 последних прогонов — они появятся
            здесь после первой синхронизации.
          </p>
        {:else}
          <ul class="runs">
            {#each history as run (run.id)}
              <li class="run {syncStatusTone(run.status)}">
                <span class="status {syncStatusTone(run.status)}">
                  {syncStatusLabel(run.status)}
                </span>
                <div class="run-main">
                  <div class="run-top">
                    <span class="run-time" title="Начало и (если есть) конец прогона">
                      {timeText(run.started_at)}
                      {#if finishClock(run.finished_at)}
                        → {finishClock(run.finished_at)}
                      {/if}
                    </span>
                    {#if triggerText(run)}
                      <span class="trigger">{triggerText(run)}</span>
                    {/if}
                    {#if durationText(run)}
                      <span class="duration">{durationText(run)}</span>
                    {/if}
                  </div>
                  <div class="run-meta">
                    {#if run.status === 'running'}
                      <span class="counts">прогон выполняется</span>
                    {:else}
                      <span
                        class="counts"
                        title={countsSummary(run.counts) ?? 'Счётчики не присланы'}
                      >
                        изменения: {countsMini(run.counts)}
                      </span>
                      {#if run.playlist_counts !== null}
                        <span
                          class="counts"
                          title={countsSummary(run.playlist_counts) ?? 'Счётчики не присланы'}
                        >
                          плейлисты: {countsMini(run.playlist_counts)}
                        </span>
                      {/if}
                    {/if}
                  </div>
                  {#if run.error}
                    <p class="run-error" title={run.error}>{run.error}</p>
                  {/if}
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="sync-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="sync-pin-title">PIN для управления синхронизацией</h2>
        <p>
          Запуск и отмена — изменяющие действия. Введите PIN: последний запрос
          повторится автоматически. PIN не сохраняется в браузере.
        </p>
        <input
          type="password"
          inputmode="numeric"
          autocomplete="current-password"
          placeholder="PIN"
          bind:value={pin}
          aria-label="PIN"
          use:focusInput
        />
        {#if pinError}<span class="pin-error" role="alert">{pinError}</span>{/if}
        <div class="modal-actions">
          <button type="button" class="ghost" onclick={cancelPin}>Отмена</button>
          <button type="submit" class="primary" disabled={!pin}>Войти</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .sync {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .sync-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 18px;
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
    max-width: 72ch;
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
    gap: 7px;
    padding: 5px 11px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .chip.state.running {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.09);
  }

  .chip.schedule {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.28);
    background: rgba(139, 92, 246, 0.08);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0.7;
  }

  .chip.state.running .dot {
    animation: blink 1.6s ease-in-out infinite;
  }

  @keyframes blink {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }

  .updated {
    color: var(--muted);
    font-size: 0.68rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  button.ghost {
    padding: 8px 14px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 700;
  }

  button.ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  button.ghost.small {
    padding: 6px 12px;
    font-size: 0.72rem;
  }

  button.ghost.danger {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.08);
  }

  button.ghost.danger:hover:not(:disabled) {
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
    line-height: 1.45;
  }

  .notice.error {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
  }

  .notice.error strong {
    color: #fecdd3;
  }

  .notice span {
    overflow-wrap: anywhere;
  }

  .notice code {
    font-family: inherit;
    font-size: 0.74rem;
    color: #c4b5fd;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
    gap: 12px;
    align-items: stretch;
  }

  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
    padding: 16px;
    border-radius: 18px;
    border: 1px solid var(--border);
    background: linear-gradient(160deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.02));
  }

  .card.now.active {
    border-color: rgba(34, 211, 238, 0.28);
    background: linear-gradient(160deg, rgba(34, 211, 238, 0.09), rgba(139, 92, 246, 0.05));
  }

  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }

  .card h2 {
    margin: 0;
    font-size: 0.94rem;
    letter-spacing: -0.01em;
  }

  .card .big {
    margin: 0;
    font-size: clamp(1.15rem, 2vw, 1.4rem);
    font-weight: 700;
    letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums;
  }

  .phase-line {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0;
    font-size: 0.8rem;
  }

  .phase {
    color: #a5f3fc;
    font-weight: 600;
  }

  .phase-count {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }

  .idle-line {
    margin: 0;
    color: var(--text);
    font-size: 0.84rem;
    font-weight: 600;
  }

  .meta {
    display: flex;
    align-items: baseline;
    gap: 6px;
    flex-wrap: wrap;
    margin: 0;
    color: var(--muted);
    font-size: 0.7rem;
    font-variant-numeric: tabular-nums;
  }

  .hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .hint code {
    font-family: inherit;
    font-size: 0.7rem;
    color: #c4b5fd;
  }

  .bar {
    display: block;
    height: 7px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.1);
    overflow: hidden;
  }

  .bar > span {
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width 240ms ease;
  }

  .bar.indeterminate > span {
    width: 35%;
    transform: translateX(-100%);
    animation: slide 1.3s ease-in-out infinite;
  }

  @keyframes slide {
    0% {
      transform: translateX(-100%);
    }
    100% {
      transform: translateX(300%);
    }
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }

  .stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    padding: 9px 10px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.5);
  }

  .stat .value {
    font-size: 1.2rem;
    font-weight: 800;
    line-height: 1.15;
    font-variant-numeric: tabular-nums;
  }

  .stat .label {
    color: var(--muted);
    font-size: 0.64rem;
  }

  .stat.added .value {
    color: #86efac;
  }

  .stat.updated .value {
    color: #a5f3fc;
  }

  .stat.removed .value {
    color: #fca5a5;
  }

  .status {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.64rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .status.idle {
    color: var(--muted);
  }

  .status.running {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.1);
  }

  .status.success {
    color: #86efac;
    border-color: rgba(134, 239, 172, 0.28);
    background: rgba(16, 185, 129, 0.09);
  }

  .status.partial {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.3);
    background: rgba(252, 211, 77, 0.08);
  }

  .status.failed {
    color: #fca5a5;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.1);
  }

  .status.cancelled {
    color: #cbd5e1;
    border-color: rgba(203, 213, 225, 0.24);
    background: rgba(203, 213, 225, 0.07);
  }

  .error-line {
    margin: 0;
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid rgba(248, 113, 113, 0.28);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
    font-size: 0.72rem;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 12px 14px;
    border-radius: 16px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.42);
  }

  .actions .primary {
    padding: 9px 18px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    color: #0b0715;
    background: var(--accent);
  }

  .actions .primary:hover:not(:disabled) {
    filter: brightness(1.06);
  }

  .actions-hint {
    flex: 1 1 260px;
    min-width: 0;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .group-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }

  .group-head h2 {
    margin: 0;
    font-size: 0.98rem;
  }

  .group-count {
    color: var(--muted);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }

  .hint-inline {
    color: var(--muted);
    font-size: 0.68rem;
  }

  .group-empty {
    margin: 0;
    padding: 12px 4px;
    color: var(--muted);
    font-size: 0.78rem;
  }

  .runs {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .run {
    display: grid;
    grid-template-columns: minmax(84px, auto) minmax(0, 1fr);
    align-items: start;
    gap: 12px;
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.02);
    transition:
      background 140ms ease,
      border-color 140ms ease;
  }

  .run:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--border);
  }

  .run.running {
    border-color: rgba(34, 211, 238, 0.2);
    background: rgba(34, 211, 238, 0.045);
  }

  .run.failed {
    border-color: rgba(248, 113, 113, 0.2);
  }

  .run-main {
    display: flex;
    flex-direction: column;
    gap: 5px;
    min-width: 0;
  }

  .run-top {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 0.72rem;
    min-width: 0;
  }

  .run-time {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .trigger {
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    font-size: 0.62rem;
    white-space: nowrap;
  }

  .duration {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .run-meta {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 0.68rem;
    min-width: 0;
  }

  .counts {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .run-error {
    margin: 0;
    color: #fecdd3;
    font-size: 0.7rem;
    line-height: 1.4;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .empty {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 22px;
    border-radius: 18px;
    border: 1px dashed var(--border);
    color: var(--muted);
    background: rgba(10, 14, 24, 0.35);
  }

  .empty svg {
    color: var(--accent-2);
    opacity: 0.75;
    flex: none;
  }

  .empty strong {
    display: block;
    color: var(--text);
    margin-bottom: 3px;
    font-size: 0.92rem;
  }

  .empty p {
    margin: 0;
    font-size: 0.78rem;
    max-width: 70ch;
    line-height: 1.5;
  }

  .skeleton-card {
    min-height: 132px;
    justify-content: center;
  }

  .skeleton-line {
    display: block;
    height: 9px;
    border-radius: 999px;
    background: linear-gradient(
      100deg,
      rgba(255, 255, 255, 0.06) 25%,
      rgba(255, 255, 255, 0.12) 45%,
      rgba(255, 255, 255, 0.06) 65%
    );
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .skeleton-line.short {
    width: 45%;
  }

  @keyframes shimmer {
    0% {
      background-position: 120% 0;
    }
    100% {
      background-position: -40% 0;
    }
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
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
    width: min(390px, 100%);
    padding: 20px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: #0d1220;
    box-shadow: 0 30px 70px -24px rgba(0, 0, 0, 0.9);
  }

  .modal h2 {
    margin: 0 0 6px;
    font-size: 1.02rem;
  }

  .modal p {
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.45;
  }

  .modal input {
    width: 100%;
    padding: 10px 12px;
    border-radius: 11px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    font-size: 0.9rem;
    letter-spacing: 0.2em;
  }

  .pin-error {
    display: block;
    margin-top: 8px;
    color: #fca5a5;
    font-size: 0.75rem;
  }

  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
  }

  .modal-actions .primary {
    padding: 8px 16px;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 700;
    color: #0b0715;
    background: var(--accent);
  }

  .modal-actions .primary:disabled {
    opacity: 0.45;
  }

  @media (max-width: 900px) {
    .sync-head {
      align-items: flex-start;
    }
    .head-actions {
      width: 100%;
    }
  }

  @media (max-width: 760px) {
    .cards {
      grid-template-columns: 1fr;
    }
    .run {
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .actions .primary,
    .actions .ghost {
      flex: 1 1 auto;
      min-height: 40px;
    }
  }

  @media (max-width: 480px) {
    .sync {
      padding: 16px;
      border-radius: 20px;
    }
    .stat {
      padding: 8px;
    }
    .stat .value {
      font-size: 1.05rem;
    }
    .empty {
      flex-direction: column;
      align-items: flex-start;
    }
    .actions .primary,
    .actions .ghost {
      width: 100%;
      min-height: 42px;
    }
    .actions-hint {
      flex-basis: 100%;
    }
  }
</style>
