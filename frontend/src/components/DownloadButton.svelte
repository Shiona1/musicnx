<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import {
    describeDownloadError,
    isRetryablePinLoginError,
    PinCancelledError,
  } from '../lib/downloads';
  import type { DownloadRequest, DownloadTask, PinPrompt } from '../lib/downloads';
  import { startDownload } from '../lib/downloadStores';

  /**
   * P4 "download to offline" button for a track, an album or a Yandex playlist.
   *
   * It never claims the file is ready: a successful POST answers `202` with
   * queue tasks, so the feedback is «в очереди», while progress/completion stay
   * owned by the server (DownloadsView + WS `download.*`).
   *
   * The mutation is PIN/localhost protected (docs/API.md §8). When the server
   * answers `401 auth_required`/`403 pin_required` the button either calls the
   * `onPinRequired` hook (the integrator's own dialog) or opens its built-in PIN
   * dialog, logs in with `POST /auth/pin` and replays the exact request.
   */
  export let trackId: number | null = null;
  export let albumId: number | null = null;
  export let playlistId: string | null = null;
  export let disabled = false;
  export let busy = false;
  /** Icon-only button for compact rows/cards. */
  export let compact = false;
  export let label = 'Скачать';
  export let onStarted: ((tasks: DownloadTask[]) => void) | null = null;
  /** Routes the feedback to the app toast; without it a local line is shown. */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: PinPrompt | null = null;

  const MAX_PIN_ROUNDS = 3;

  let working = false;
  let localMessage = '';
  let localKind: 'ok' | 'error' = 'ok';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;

  $: request = buildRequest(trackId, albumId, playlistId);
  $: targetName =
    trackId !== null ? 'трек' : albumId !== null ? 'альбом' : playlistId ? 'плейлист' : '';
  $: controlDisabled = disabled || busy || working || request === null;
  $: buttonTitle =
    request === null
      ? 'Не выбран объект для скачивания'
      : disabled || busy
        ? 'Дождитесь завершения текущего действия'
        : `Скачать ${targetName} для офлайн-доступа`;

  function buildRequest(
    track: number | null,
    album: number | null,
    playlist: string | null,
  ): DownloadRequest | null {
    // Documented precedence: one target per request (docs/API.md §8.2).
    if (track !== null) return { track_id: track };
    if (album !== null) return { album_id: album };
    if (playlist !== null && playlist !== '') return { playlist_id: playlist };
    return null;
  }

  function notify(text: string, kind: 'ok' | 'error' = 'ok'): void {
    if (onToast) {
      localMessage = '';
      onToast(text, kind === 'error' ? 'error' : 'info');
      return;
    }
    localMessage = text;
    localKind = kind;
  }

  /** Asks for the PIN: integrator hook first, built-in dialog otherwise. */
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

  async function download(): Promise<void> {
    const payload = request;
    // `working` is checked directly: the reactive `controlDisabled` may still
    // hold its previous value within the same event loop tick.
    if (working || !payload || disabled || busy) return;
    working = true;
    localMessage = '';
    pinError = '';
    try {
      for (let round = 0; round < MAX_PIN_ROUNDS; round += 1) {
        try {
          const tasks = await startDownload(payload, askPin);
          pinError = '';
          notify(startedText(tasks));
          onStarted?.(tasks);
          return;
        } catch (error) {
          if (error instanceof PinCancelledError) {
            notify('Скачивание отменено: PIN не введён.', 'error');
            return;
          }
          if (isRetryablePinLoginError(error) && !onPinRequired) {
            // Keep the built-in dialog going with the exact reason.
            pinError = describeDownloadError(error);
            continue;
          }
          notify(describeDownloadError(error), 'error');
          return;
        }
      }
      notify('PIN не принят: задача не создана.', 'error');
    } finally {
      working = false;
    }
  }

  /**
   * The POST answer is the server's state, not a promise that a new row was
   * created: `DownloadManager.start_track` returns the existing task for a
   * duplicate (queued/downloading/completed). The message therefore reports
   * statuses instead of claiming «добавлено» (AGENTS.md §12).
   */
  function startedText(tasks: DownloadTask[]): string {
    if (tasks.length === 0) {
      return 'Сервер не вернул задач: возможно, всё уже скачано и проверено.';
    }
    if (tasks.length === 1) {
      const task = tasks[0];
      if (task.status === 'completed') {
        return 'Трек уже скачан и проверен: файл доступен офлайн.';
      }
      if (task.status === 'queued' || task.status === 'downloading' || task.status === 'paused') {
        return 'Задача в очереди загрузок.';
      }
      if (task.status === 'failed') {
        return 'Сервер сообщил ошибку загрузки — подробности в разделе «Загрузки».';
      }
      if (task.status === 'cancelled') {
        return 'Сервер считает задачу отменённой — повторите её в разделе «Загрузки».';
      }
    }
    const queued = tasks.filter(
      (task) => task.status === 'queued' || task.status === 'downloading' || task.status === 'paused',
    ).length;
    const verified = tasks.filter((task) => task.status === 'completed').length;
    const parts: string[] = [];
    if (queued > 0) parts.push(`в очереди: ${queued}`);
    if (verified > 0) parts.push(`уже проверено: ${verified}`);
    if (parts.length === 0) parts.push(`задач: ${tasks.length}`);
    return `Запрос принят — ${parts.join(', ')}.`;
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<span class="download-wrap">
  <button
    type="button"
    class="download"
    class:compact
    class:pending={working}
    disabled={controlDisabled}
    title={buttonTitle}
    aria-label={working ? `Скачиваем ${targetName}` : `Скачать ${targetName}`}
    aria-busy={working}
    onclick={download}
  >
    {#if working}
      <span class="spinner" aria-hidden="true"></span>
    {:else}
      <svg
        viewBox="0 0 24 24"
        width="16"
        height="16"
        fill="currentColor"
        aria-hidden="true"
        focusable="false"
      >
        <path d="M11 3h2v9.17l3.59-3.58L18 10l-6 6-6-6 1.41-1.41L11 12.17V3zM5 19h14v2H5z" />
      </svg>
    {/if}
    {#if !compact}
      <span class="label">{working ? 'В очередь…' : label}</span>
    {/if}
  </button>

  {#if localMessage}
    <span class="message" class:error={localKind === 'error'} role="status" aria-live="polite">
      {localMessage}
    </span>
  {/if}
</span>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="download-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="download-pin-title">PIN для загрузки</h2>
        <p>
          Скачивание — изменяющее действие, оно защищено PIN-кодом сервера.
          Введите PIN: запрос повторится автоматически.
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
  .download-wrap {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 5px;
    min-width: 0;
  }

  .download {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 7px 13px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 700;
    max-width: 100%;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      color 140ms ease,
      transform 140ms ease;
  }

  .download:hover:not(:disabled) {
    background: var(--surface-hover);
    border-color: rgba(34, 211, 238, 0.35);
    color: #a5f3fc;
    transform: translateY(-1px);
  }

  .download.compact {
    padding: 0;
    width: 30px;
    height: 30px;
    justify-content: center;
    border-radius: 9px;
    border-color: transparent;
    background: none;
    color: var(--muted);
  }

  .download.compact:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--border);
  }

  /* Touch devices: the compact control meets the 44x44px target floor. */
  @media (pointer: coarse) {
    .download.compact {
      width: 44px;
      height: 44px;
    }
  }

  .download.pending {
    color: #a5f3fc;
  }

  .label {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .spinner {
    width: 13px;
    height: 13px;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.18);
    border-top-color: var(--accent-2);
    animation: spin 0.9s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .message {
    font-size: 0.68rem;
    line-height: 1.35;
    color: var(--muted);
    max-width: 32ch;
  }

  .message.error {
    color: #fecdd3;
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
    width: min(380px, 100%);
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

  .modal-actions .ghost {
    padding: 8px 14px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 700;
  }

  .modal-actions .ghost:hover {
    background: var(--surface-hover);
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

  @media (max-width: 560px) {
    .download:not(.compact) {
      padding: 9px 15px;
      min-height: 40px;
    }
    .download.compact {
      width: 34px;
      height: 34px;
    }
    .message {
      max-width: 100%;
    }
  }
</style>
