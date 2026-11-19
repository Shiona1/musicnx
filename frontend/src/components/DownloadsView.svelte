<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onMount } from 'svelte';
  import {
    DOWNLOAD_STATUS_LABELS,
    describeDownloadError,
    formatBadge,
    formatBytes,
    formatTaskTime,
    isActiveStatus,
    isRetryablePinLoginError,
    PinCancelledError,
    progressPercent,
    qualityNotice,
    withPinRetry,
  } from '../lib/downloads';
  import type { DownloadTask, PinPrompt } from '../lib/downloads';
  import {
    activeDownloads,
    cancelDownloadTask,
    deleteDownloadTask,
    downloadsError,
    downloadsLoading,
    downloadsRouteMissing,
    downloadsTotal,
    downloadsUpdatedAt,
    historyDownloads,
    refreshDownloads,
    retryDownloadTask,
    startDownloadsPolling,
  } from '../lib/downloadStores';
  import { settingsApi, describeSettingsError } from '../lib/settings';

  const QUALITY_KEY = 'downloads.quality';

  /**
   * P4 Downloads screen: queue + history of `docs/API.md §8` tasks.
   *
   * Every number on screen comes from the server's DownloadTask: status,
   * bytes, attempts and error. The format badge shows the *actual* container
   * (`format`), never the requested `quality`, so a FLAC request that fell back
   * to M4A is labelled honestly (AGENTS.md §12).
   *
   * The view polls `GET /downloads` once per second while tasks are active and
   * otherwise refreshes on demand; the integrator additionally forwards WS
   * `download.*` events through `applyDownloadEvent` (lib/downloadStores.ts).
   * Cancel/retry/delete are PIN-protected mutations; the PIN is requested by the
   * `onPinRequired` hook or the built-in dialog and never stored.
   */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: PinPrompt | null = null;

  const MAX_PIN_ROUNDS = 3;

  let busyRowId: number | null = null;
  let actionMessage = '';
  let actionKind: 'ok' | 'error' = 'ok';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let quality = 'auto';
  let qualityEditable = false;
  let qualityBusy = false;
  let qualityError = '';

  $: activeCount = $activeDownloads.length;
  $: historyCount = $historyDownloads.length;
  $: noTasks = activeCount === 0 && historyCount === 0;
  $: empty = !$downloadsLoading && noTasks;
  $: shownTotal = activeCount + historyCount;
  $: listHint =
    $downloadsTotal > shownTotal ? `показано ${shownTotal} из ${$downloadsTotal}` : '';

  onMount(() => {
    const stopPolling = startDownloadsPolling();
    void loadQuality();
    return stopPolling;
  });

  async function loadQuality(): Promise<void> {
    try {
      const snapshot = await settingsApi.get();
      const value = snapshot.values[QUALITY_KEY];
      if (typeof value === 'string') quality = value;
      qualityEditable = snapshot.editable.includes(QUALITY_KEY);
    } catch {
      // A P8-less backend simply hides the selector; downloads still work with
      // the server's config default.
      qualityEditable = false;
    }
  }

  async function saveQuality(): Promise<void> {
    qualityBusy = true;
    qualityError = '';
    try {
      await settingsApi.update({ [QUALITY_KEY]: quality });
      notify(`Качество новых загрузок: ${qualityLabel(quality)}.`);
    } catch (error) {
      qualityError = describeSettingsError(error);
      notify(qualityError, 'error');
    } finally {
      qualityBusy = false;
    }
  }

  function qualityLabel(value: string): string {
    if (value === 'flac') return 'FLAC / lossless';
    if (value === 'aac') return 'AAC';
    if (value === 'mp3') return 'MP3';
    return 'авто (лучшее доступное)';
  }

  function notify(text: string, kind: 'ok' | 'error' = 'ok'): void {
    if (onToast) {
      actionMessage = '';
      onToast(text, kind === 'error' ? 'error' : 'info');
      return;
    }
    actionMessage = text;
    actionKind = kind;
  }

  function taskLabel(task: DownloadTask): string {
    const title = task.title?.trim();
    return title ? `«${title}»` : `#${task.track_id}`;
  }

  function percent(task: DownloadTask): number | null {
    return progressPercent(task);
  }

  function bytesText(task: DownloadTask): string {
    const total = task.bytes_total === null ? '—' : formatBytes(task.bytes_total);
    return `${formatBytes(task.bytes_done)} / ${total}`;
  }

  function canCancel(task: DownloadTask): boolean {
    return isActiveStatus(task.status);
  }

  function canRetry(task: DownloadTask): boolean {
    // "retry" resets attempts/error and re-queues, so it also resumes a paused task.
    return task.status === 'failed' || task.status === 'cancelled' || task.status === 'paused';
  }

  function statusLabel(task: DownloadTask): string {
    // The payload is trusted for shape, but an unexpected status is shown raw
    // instead of rendering an empty chip.
    return DOWNLOAD_STATUS_LABELS[task.status] ?? task.status;
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
        await withPinRetry(operation, askPin);
        return { ok: true };
      } catch (error) {
        if (error instanceof PinCancelledError) {
          return { ok: false, message: 'Действие отменено: PIN не введён.' };
        }
        if (isRetryablePinLoginError(error) && !onPinRequired) {
          pinError = describeDownloadError(error);
          continue;
        }
        return { ok: false, message: describeDownloadError(error) };
      }
    }
    return { ok: false, message: 'PIN не принят: действие не выполнено.' };
  }

  type Action = 'cancel' | 'retry' | 'delete';

  // Two-step confirmation for the destructive delete action, per task row.
  let confirmDeleteId: number | null = null;

  async function runTaskAction(task: DownloadTask, action: Action): Promise<void> {
    if (busyRowId !== null) return;
    busyRowId = task.id;
    actionMessage = '';
    pinError = '';
    const label = taskLabel(task);
    // The PIN layer lives in mutateWithPin only: store actions rethrow
    // `auth_required` so the retry happens exactly once, after login.
    const operation = async (): Promise<void> => {
      if (action === 'cancel') await cancelDownloadTask(task.id);
      else if (action === 'retry') await retryDownloadTask(task.id);
      else await deleteDownloadTask(task.id);
    };
    try {
      const outcome = await mutateWithPin(operation);
      if (!outcome.ok) {
        notify(outcome.message, 'error');
        return;
      }
      if (action === 'cancel') notify(`Задача ${label} отменена.`);
      else if (action === 'retry') notify(`Задача ${label} снова в очереди.`);
      else notify(`Запись ${label} удалена из загрузок.`);
      await refreshDownloads();
    } finally {
      busyRowId = null;
    }
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }

  /** Live freshness stamp while the 1 s loop is running. */
  function clock(ms: number): string {
    return new Date(ms).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section class="downloads material-content" aria-labelledby="downloads-title">
  <header class="downloads-head">
    <div>
      <span class="eyebrow">Яндекс Музыка</span>
      <h1 id="downloads-title">Загрузки</h1>
      <p class="sub">
        Очередь и история скачивания на сервер. Скачивает только backend —
        токен Яндекса в браузер не попадает. Бейдж формата показывает
        фактический контейнер, а не обещанное качество.
      </p>
    </div>
    <div class="head-actions">
      <label class="quality-select" title="Качество, в котором скачиваются новые треки">
        <span>Качество</span>
        <select
          bind:value={quality}
          disabled={!qualityEditable || qualityBusy}
          aria-label="Качество скачивания"
          onchange={saveQuality}
        >
          <option value="auto">Авто (лучшее)</option>
          <option value="flac">FLAC / lossless</option>
          <option value="aac">AAC</option>
          <option value="mp3">MP3</option>
        </select>
      </label>
      <span class="chip queue" title="Задачи, которые ещё не завершены">В очереди: {activeCount}</span>
      <span class="chip">История: {historyCount}</span>
      {#if $downloadsUpdatedAt > 0 && activeCount > 0}
        <span class="updated" title="Последнее успешное обновление очереди по REST">
          обновлено {clock($downloadsUpdatedAt)}
        </span>
      {/if}
      <button
        type="button"
        class="ghost"
        disabled={$downloadsLoading}
        onclick={() => void refreshDownloads()}
      >
        Обновить
      </button>
    </div>
  </header>

  {#if actionMessage}
    <div class="notice" class:error={actionKind === 'error'} role={actionKind === 'error' ? 'alert' : 'status'}>
      {actionMessage}
    </div>
  {/if}

  {#if $downloadsRouteMissing}
    <div class="notice error" role="alert">
      <strong>Сервер не поддерживает загрузки.</strong>
      <span>
        <code>GET /api/v1/downloads</code> вернул 404: в этой сборке backend нет
        маршрутов P4. Кнопки «Скачать» тоже не сработают, а fake-очереди здесь
        нет — включите P4 backend и повторите.
      </span>
      <button type="button" class="ghost small" onclick={() => void refreshDownloads()}>
        Повторить
      </button>
    </div>
  {:else if $downloadsError}
    <div class="notice error" role="alert">
      <span>{$downloadsError}</span>
      <button type="button" class="ghost small" onclick={() => void refreshDownloads()}>
        Повторить
      </button>
    </div>
  {/if}

  {#if !$downloadsRouteMissing && $downloadsLoading && noTasks}
    <div class="skeletons" aria-hidden="true">
      {#each [0, 1, 2] as key (key)}
        <div class="skeleton">
          <span class="skeleton-badge"></span>
          <span class="skeleton-main">
            <span class="skeleton-line"></span>
            <span class="skeleton-line short"></span>
          </span>
        </div>
      {/each}
    </div>
    <span class="sr-only" role="status">Загружаем очередь загрузок…</span>
  {:else if !$downloadsRouteMissing && empty && !$downloadsError}
    <div class="empty">
      <svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor" aria-hidden="true">
        <path d="M11 3h2v9.17l3.59-3.58L18 10l-6 6-6-6 1.41-1.41L11 12.17V3zM5 19h14v2H5z" />
      </svg>
      <div>
        <strong>Загрузок пока нет</strong>
        <p>
          Поставьте трек, альбом или плейлист в очередь кнопкой «Скачать» —
          задача появится здесь вместе с прогрессом и фактическим форматом.
          Офлайн-статус трек получает только после проверки файла на сервере.
        </p>
      </div>
    </div>
  {:else if !$downloadsRouteMissing}
    {#if listHint}
      <p class="list-hint">{listHint}</p>
    {/if}

    <div class="group">
      <div class="group-head">
        <h2>Очередь</h2>
        <span class="group-count">{activeCount}</span>
      </div>
      {#if activeCount === 0}
        <p class="group-empty">Активных загрузок нет.</p>
      {:else}
        <ul class="task-list">
          {#each $activeDownloads as task (task.id)}
            <li class="task active {task.status}">
              <span
                class="format"
                class:unknown={!task.format}
                title={task.format
                  ? 'Фактический формат файла (не обещанное качество)'
                  : 'Фактический формат ещё не определён'}
              >
                {formatBadge(task)}
              </span>

              <div class="task-main">
                <div class="task-top">
                  <span class="task-title" title={task.title ?? ''}>{task.title ?? `Трек #${task.track_id}`}</span>
                  <span class="status {task.status}">{statusLabel(task)}</span>
                </div>

                <div class="task-meta">
                  {#if qualityNotice(task)}
                    <span class="quality" title="Запрошенное качество; итоговый формат — в бейдже">
                      {qualityNotice(task)}
                    </span>
                  {/if}
                  {#if task.attempts > 0}
                    <span class="attempts">попыток: {task.attempts}</span>
                  {/if}
                  {#if task.target_path}
                    <span class="target" title={task.target_path}>{task.target_path}</span>
                  {/if}
                </div>

                <div class="progress-row">
                  {#if percent(task) !== null}
                    <span
                      class="bar"
                      role="progressbar"
                      aria-valuemin="0"
                      aria-valuemax="100"
                      aria-valuenow={percent(task) ?? 0}
                      aria-valuetext={`${percent(task)}% · ${bytesText(task)}`}
                      aria-label={`Прогресс загрузки ${task.title ?? `трека #${task.track_id}`}`}
                    >
                      <span style={`width: ${percent(task)}%`}></span>
                    </span>
                    <span class="bytes">{bytesText(task)}</span>
                    <span class="percent">{percent(task)}%</span>
                  {:else}
                    <span class="bar waiting" aria-hidden="true"><span></span></span>
                    <span class="bytes">{bytesText(task)}</span>
                  {/if}
                </div>

                {#if task.error}
                  <p class="task-error" role="note">{task.error}</p>
                {/if}
              </div>

              <div class="task-actions">
                {#if canCancel(task)}
                  <button
                    type="button"
                    class="ghost small"
                    disabled={busyRowId !== null}
                    title="Мягкая отмена: частичный файл сохраняется для докачки"
                    aria-label={`Отменить загрузку ${task.title ?? `трека #${task.track_id}`}`}
                    onclick={() => void runTaskAction(task, 'cancel')}
                  >
                    Отменить
                  </button>
                {/if}
                {#if canRetry(task)}
                  <button
                    type="button"
                    class="ghost small"
                    disabled={busyRowId !== null}
                    title="Сбросить ошибку и снова поставить в очередь"
                    aria-label={`Повторить загрузку ${task.title ?? `трека #${task.track_id}`}`}
                    onclick={() => void runTaskAction(task, 'retry')}
                  >
                    Повторить
                  </button>
                {/if}
                {#if confirmDeleteId === task.id}
                  <span class="confirm" role="group" aria-label="Подтверждение удаления записи">
                    Удалить?
                    <button
                      type="button"
                      class="ghost small danger"
                      disabled={busyRowId !== null}
                      onclick={() => {
                        confirmDeleteId = null;
                        void runTaskAction(task, 'delete');
                      }}
                    >
                      Да, удалить
                    </button>
                    <button
                      type="button"
                      class="ghost small"
                      onclick={() => (confirmDeleteId = null)}
                    >
                      Нет
                    </button>
                  </span>
                {:else}
                  <button
                    type="button"
                    class="ghost small danger"
                    disabled={busyRowId !== null}
                    title={canCancel(task)
                      ? 'Отменить активную загрузку и удалить запись из очереди'
                      : 'Удалить запись из истории загрузок'}
                    aria-label={`Удалить задачу ${task.title ?? `трека #${task.track_id}`}`}
                    onclick={() => (confirmDeleteId = task.id)}
                  >
                    Удалить
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <div class="group">
      <div class="group-head">
        <h2>История</h2>
        <span class="group-count">{historyCount}</span>
      </div>
      {#if historyCount === 0}
        <p class="group-empty">
          Завершённых и ошибочных задач нет. История появится, когда первая
          загрузка завершится или отменится.
        </p>
      {:else}
        <ul class="task-list">
          {#each $historyDownloads as task (task.id)}
            <li class="task {task.status}">
              <span
                class="format"
                class:unknown={!task.format}
                title={task.format
                  ? 'Фактический формат файла (не обещанное качество)'
                  : 'Фактический формат ещё не определён'}
              >
                {formatBadge(task)}
              </span>

              <div class="task-main">
                <div class="task-top">
                  <span class="task-title" title={task.title ?? ''}>{task.title ?? `Трек #${task.track_id}`}</span>
                  <span class="status {task.status}">{statusLabel(task)}</span>
                </div>

                <div class="task-meta">
                  {#if qualityNotice(task)}
                    <span class="quality" title="Запрошенное качество; итоговый формат — в бейдже">
                      {qualityNotice(task)}
                    </span>
                  {/if}
                  {#if task.attempts > 0}
                    <span class="attempts">попыток: {task.attempts}</span>
                  {/if}
                  {#if task.target_path}
                    <span class="target" title={task.target_path}>{task.target_path}</span>
                  {/if}
                  <span class="stamp" title="Последнее обновление задачи">{formatTaskTime(task.updated_at)}</span>
                </div>

                <div class="progress-row">
                  {#if task.status === 'completed'}
                    <span class="bytes">файл проверен · {bytesText(task)}</span>
                  {:else if percent(task) !== null}
                    <span class="bytes">записано {percent(task)}% · {bytesText(task)}</span>
                  {:else}
                    <span class="bytes">{bytesText(task)}</span>
                  {/if}
                </div>

                {#if task.error}
                  <p class="task-error" role="note">{task.error}</p>
                {/if}
              </div>

              <div class="task-actions">
                {#if canRetry(task)}
                  <button
                    type="button"
                    class="ghost small"
                    disabled={busyRowId !== null}
                    title="Сбросить ошибку и снова поставить в очередь"
                    aria-label={`Повторить загрузку ${task.title ?? `трека #${task.track_id}`}`}
                    onclick={() => void runTaskAction(task, 'retry')}
                  >
                    Повторить
                  </button>
                {/if}
                {#if confirmDeleteId === task.id}
                  <span class="confirm" role="group" aria-label="Подтверждение удаления записи">
                    Удалить?
                    <button
                      type="button"
                      class="ghost small danger"
                      disabled={busyRowId !== null}
                      onclick={() => {
                        confirmDeleteId = null;
                        void runTaskAction(task, 'delete');
                      }}
                    >
                      Да, удалить
                    </button>
                    <button
                      type="button"
                      class="ghost small"
                      onclick={() => (confirmDeleteId = null)}
                    >
                      Нет
                    </button>
                  </span>
                {:else}
                  <button
                    type="button"
                    class="ghost small danger"
                    disabled={busyRowId !== null}
                    title="Удалить запись из истории загрузок"
                    aria-label={`Удалить задачу ${task.title ?? `трека #${task.track_id}`}`}
                    onclick={() => (confirmDeleteId = task.id)}
                  >
                    Удалить
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="downloads-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="downloads-pin-title">PIN для управления загрузками</h2>
        <p>
          Отмена, повтор и удаление задач — изменяющие действия. Введите PIN:
          последний запрос повторится автоматически.
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
  .downloads {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .downloads-head {
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
    max-width: 66ch;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .quality-select {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 4px 3px 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface);
    color: var(--muted);
    font-size: 0.68rem;
    font-weight: 600;
  }

  .quality-select select {
    padding: 4px 7px;
    border: 0;
    border-radius: 999px;
    background: var(--surface-hover);
    color: var(--text);
    font-size: 0.7rem;
    font-weight: 700;
  }

  .quality-select select:disabled {
    opacity: 0.55;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    padding: 5px 11px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .chip.queue {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.28);
    background: rgba(34, 211, 238, 0.08);
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

  .group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .group-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
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

  .group-empty {
    margin: 0;
    padding: 12px 4px;
    color: var(--muted);
    font-size: 0.78rem;
  }

  .list-hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.72rem;
  }

  .task-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .task {
    display: grid;
    grid-template-columns: 52px minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    border-radius: 15px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.02);
    transition:
      background 140ms ease,
      border-color 140ms ease;
  }

  .task:hover,
  .task:focus-within {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--border);
  }

  .task.active {
    border-color: rgba(34, 211, 238, 0.2);
    background: rgba(34, 211, 238, 0.045);
  }

  .task.failed {
    border-color: rgba(248, 113, 113, 0.22);
  }

  .format {
    display: grid;
    place-items: center;
    width: 52px;
    height: 52px;
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    background: linear-gradient(140deg, rgba(139, 92, 246, 0.24), rgba(34, 211, 238, 0.12));
    color: #ddd6fe;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-align: center;
    overflow-wrap: anywhere;
  }

  .format.unknown {
    color: var(--muted);
    background: var(--surface);
  }

  .task-main {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .task-top {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;
  }

  .task-title {
    font-size: 0.86rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .status {
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.64rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .status.downloading {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.1);
  }

  .status.queued {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.32);
    background: rgba(139, 92, 246, 0.1);
  }

  .status.paused {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.3);
    background: rgba(252, 211, 77, 0.08);
  }

  .status.completed {
    color: #86efac;
    border-color: rgba(134, 239, 172, 0.28);
    background: rgba(16, 185, 129, 0.09);
  }

  .status.failed {
    color: #fca5a5;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.1);
  }

  .task-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 0.68rem;
    min-width: 0;
  }

  .quality {
    color: #c4b5fd;
  }

  .target {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
    opacity: 0.85;
  }

  .progress-row {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 0.68rem;
    color: var(--muted);
    flex-wrap: wrap;
  }

  .bar {
    display: block;
    flex: 1 1 120px;
    min-width: 90px;
    height: 6px;
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

  .bar.waiting > span {
    width: 0;
  }

  .bytes,
  .percent {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .percent {
    color: #a5f3fc;
    font-weight: 600;
  }

  .task-error {
    margin: 0;
    color: #fecdd3;
    font-size: 0.7rem;
    overflow-wrap: anywhere;
  }

  .task-actions {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
    justify-content: flex-end;
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

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .skeleton {
    display: grid;
    grid-template-columns: 52px minmax(0, 1fr);
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
  }

  .skeleton-badge,
  .skeleton-line {
    display: block;
    border-radius: 11px;
    background: linear-gradient(
      100deg,
      rgba(255, 255, 255, 0.06) 25%,
      rgba(255, 255, 255, 0.12) 45%,
      rgba(255, 255, 255, 0.06) 65%
    );
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .skeleton-badge {
    width: 52px;
    height: 52px;
  }

  .skeleton-main {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .skeleton-line {
    height: 9px;
    border-radius: 999px;
  }

  .skeleton-line.short {
    width: 55%;
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

  @media (max-width: 760px) {
    .task {
      grid-template-columns: 46px minmax(0, 1fr);
      align-items: start;
    }
    .format {
      width: 46px;
      height: 46px;
      font-size: 0.64rem;
    }
    .task-actions {
      grid-column: 1 / -1;
      justify-content: flex-start;
      padding-left: 58px;
    }
    .downloads-head {
      align-items: flex-start;
    }
    .head-actions {
      width: 100%;
    }
  }

  @media (max-width: 480px) {
    .task-actions {
      padding-left: 0;
    }
    .task-actions button {
      min-height: 44px;
    }
    .empty {
      flex-direction: column;
      align-items: flex-start;
    }
  }

  /* Touch confirmation buttons must clear the 44px target floor too. */
  .confirm button {
    min-height: 44px;
  }

  .confirm {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #fecdd3;
    font-size: 0.7rem;
    font-weight: 600;
  }
</style>
