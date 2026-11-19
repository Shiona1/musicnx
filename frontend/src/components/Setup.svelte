<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount } from 'svelte';
  import { ApiError, yandexApi } from '../lib/yandex';
  import type { DeviceStart, DeviceState, YandexAuthStatus } from '../lib/yandex';
  import {
    applyYandexAuth,
    describeYandexError,
    refreshYandexAuth,
    resetYandexStores,
    yandexAuth,
    yandexAuthError,
    yandexAuthLoading,
  } from '../lib/yandexStores';

  /**
   * P3 Setup screen: Device Flow with QR + polling, manual token, logout.
   *
   * PIN-protected mutations answer 401 auth_required / 403 pin_required; the
   * dialog collects the PIN through POST /auth/pin and then replays the exact
   * operation. No token is ever stored client-side: the server keeps it in a
   * 0600 file and the browser only holds the HttpOnly session cookie.
   */
  export let onAuthChange: (auth: YandexAuthStatus | null) => void = () => {};
  export let onClose: (() => void) | null = null;

  const EXPIRED_TEXT = 'Срок действия кода истёк. Начните вход заново.';
  const POLL_FAILURE_LIMIT = 3;
  /** 401/403 codes that are not about the mini-PIN and must not open the dialog. */
  const PIN_EXEMPT_CODES = new Set([
    'origin_forbidden',
    'host_forbidden',
    'invalid_token',
    'invalid_pin',
    'too_many_attempts',
    'network_error',
    'pin_required',
    'pin_not_configured',
  ]);
  const DEVICE_STATE_LABELS: Record<DeviceState, string> = {
    pending: 'Ожидаем подтверждение',
    authorized: 'Вход подтверждён',
    expired: 'Код истёк',
    denied: 'Доступ отклонён',
    error: 'Ошибка входа',
  };

  let device: DeviceStart | null = null;
  let deviceState: DeviceState | null = null;
  let deviceError = '';
  let deviceLogin: string | null = null;
  let starting = false;
  let remaining = 0;
  let pollFailures = 0;
  let deadline = 0;
  let pollTimer: number | undefined;
  let ticker: number | undefined;
  let disposed = false;

  let tokenValue = '';
  let tokenBusy = false;
  let tokenError = '';

  let logoutBusy = false;
  // Two-step confirmation: disconnecting deletes the server-side token.
  let confirmLogout = false;
  let actionError = '';

  let showPin = false;
  let pin = '';
  let pinBusy = false;
  let pinError = '';
  let pending: { run: () => Promise<void>; fail: (error: unknown) => void } | null = null;

  $: authenticated = $yandexAuth?.authenticated === true;
  $: if (authenticated) stopTimers();
  $: deviceStateText = deviceState ? DEVICE_STATE_LABELS[deviceState] : '';
  $: onAuthChange($yandexAuth);

  onMount(() => {
    void refreshYandexAuth();
    return () => {
      disposed = true;
      stopTimers();
    };
  });

  onDestroy(() => {
    disposed = true;
    stopTimers();
  });

  function needsPinPrompt(error: unknown): boolean {
    if (!(error instanceof ApiError)) return false;
    if (error.status !== 401 && error.status !== 403) return false;
    return !PIN_EXEMPT_CODES.has(error.code);
  }

  async function withPinRetry(
    run: () => Promise<void>,
    fail: (error: unknown) => void,
  ): Promise<void> {
    try {
      await run();
    } catch (error) {
      if (needsPinPrompt(error)) {
        pending = { run, fail };
        pin = '';
        pinError = '';
        showPin = true;
        return;
      }
      fail(error);
    }
  }

  function closePin(): void {
    showPin = false;
    pin = '';
    pinError = '';
    pending = null;
  }

  async function submitPin(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!pin || pinBusy) return;
    pinBusy = true;
    pinError = '';
    try {
      await yandexApi.loginPin(pin);
      showPin = false;
      pin = '';
      const queued = pending;
      pending = null;
      if (queued) await queued.run().catch(queued.fail);
    } catch (error) {
      pinError = describeYandexError(error);
    } finally {
      pinBusy = false;
    }
  }

  function clearPoll(): void {
    if (pollTimer !== undefined) {
      window.clearTimeout(pollTimer);
      pollTimer = undefined;
    }
  }

  function clearTicker(): void {
    if (ticker !== undefined) {
      window.clearInterval(ticker);
      ticker = undefined;
    }
  }

  function stopTimers(): void {
    clearPoll();
    clearTicker();
  }

  function pollDelayMs(): number {
    const seconds = device?.interval ?? 5;
    return Math.min(30, Math.max(1, seconds)) * 1000;
  }

  function schedulePoll(delayMs: number): void {
    clearPoll();
    if (disposed) return;
    pollTimer = window.setTimeout(() => {
      pollTimer = undefined;
      void pollDevice();
    }, Math.max(300, delayMs));
  }

  function startTicker(): void {
    clearTicker();
    ticker = window.setInterval(() => {
      remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      if (remaining === 0 && deviceState === 'pending') markExpired();
    }, 1000);
  }

  function markExpired(): void {
    stopTimers();
    deviceState = 'expired';
    deviceError = EXPIRED_TEXT;
  }

  async function startDevice(): Promise<void> {
    if (starting) return;
    deviceError = '';
    deviceState = null;
    deviceLogin = null;
    stopTimers();
    await withPinRetry(
      async () => {
        starting = true;
        try {
          const start = await yandexApi.deviceStart();
          device = start;
          deviceState = 'pending';
          pollFailures = 0;
          remaining = Math.max(0, Math.ceil(start.expires_in));
          deadline = Date.now() + Math.max(30, start.expires_in) * 1000;
          startTicker();
          schedulePoll(pollDelayMs());
        } finally {
          starting = false;
        }
      },
      (error) => {
        starting = false;
        deviceState = 'error';
        deviceError = describeYandexError(error);
      },
    );
  }

  async function pollDevice(): Promise<void> {
    if (disposed || !device || deviceState !== 'pending') return;
    if (Date.now() > deadline) {
      markExpired();
      return;
    }
    try {
      const status = await yandexApi.deviceStatus(device.flow_id);
      if (disposed) return;
      pollFailures = 0;
      deviceState = status.state;
      deviceLogin = status.login;
      if (status.state === 'pending') {
        schedulePoll(pollDelayMs());
        return;
      }
      stopTimers();
      if (status.state === 'authorized') {
        deviceError = '';
        await refreshYandexAuth();
      } else if (status.state === 'error') {
        deviceError = status.error ?? 'Не удалось завершить вход. Попробуйте ещё раз.';
      }
    } catch (error) {
      if (disposed) return;
      pollFailures += 1;
      if (pollFailures >= POLL_FAILURE_LIMIT) {
        stopTimers();
        deviceState = 'error';
        deviceError = describeYandexError(error);
        return;
      }
      schedulePoll(Math.min(pollDelayMs() * pollFailures, 15000));
    }
  }

  async function submitToken(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!tokenValue || tokenBusy) return;
    tokenBusy = true;
    tokenError = '';
    const token = tokenValue;
    await withPinRetry(
      async () => {
        const status = await yandexApi.loginToken(token);
        tokenValue = '';
        resetYandexStores();
        applyYandexAuth(status);
      },
      (error) => {
        tokenError = describeYandexError(error);
      },
    );
    tokenBusy = false;
  }

  async function logout(): Promise<void> {
    if (logoutBusy) return;
    logoutBusy = true;
    actionError = '';
    await withPinRetry(
      async () => {
        await yandexApi.logout();
        resetYandexStores();
        stopTimers();
        device = null;
        deviceState = null;
        deviceError = '';
        deviceLogin = null;
        await refreshYandexAuth();
      },
      (error) => {
        actionError = describeYandexError(error);
      },
    );
    logoutBusy = false;
  }

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && showPin) closePin();
  }

  function formatRemaining(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}:${String(rest).padStart(2, '0')}`;
  }

  function formatExpiry(value: string | null): string {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function sourceLabel(source: string | null): string {
    if (source === 'device') return 'Device Flow (QR/код)';
    if (source === 'manual') return 'токен вручную';
    return source ?? '—';
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<section class="setup material-content" aria-labelledby="setup-title">
  <header class="setup-head">
    <div>
      <span class="eyebrow">Яндекс Музыка</span>
      <h1 id="setup-title">Подключение аккаунта</h1>
      <p class="sub">
        Device Flow с QR-кодом или ручной токен. Токен хранится только на сервере
        (файл 0600) — браузер получает лишь HttpOnly-cookie сессии, в
        localStorage ничего не пишется.
      </p>
    </div>
    {#if onClose}
      <button type="button" class="ghost" onclick={() => onClose?.()}>Готово</button>
    {/if}
  </header>

  {#if $yandexAuthError}
    <div class="notice error" role="alert">{$yandexAuthError}</div>
  {/if}
  {#if actionError}
    <div class="notice error" role="alert">{actionError}</div>
  {/if}

  {#if $yandexAuthLoading && !$yandexAuth}
    <div class="skeleton" role="status">Проверяем сессию Яндекса…</div>
  {:else if authenticated}
    <section class="panel signed-in" aria-labelledby="signed-title">
      <div class="signed-head">
        <span class="badge-ok">Подключено</span>
        <h2 id="signed-title">Аккаунт Яндекс Музыки</h2>
        <p class="login-line">{$yandexAuth?.login ?? 'логин не сообщён сервером'}</p>
      </div>

      <dl class="facts">
        <div>
          <dt>Источник входа</dt>
          <dd>{sourceLabel($yandexAuth?.source ?? null)}</dd>
        </div>
        <div>
          <dt>Токен действует до</dt>
          <dd>{formatExpiry($yandexAuth?.expires_at ?? null)}</dd>
        </div>
      </dl>

      <div class="row-actions">
        <button
          type="button"
          class="ghost"
          disabled={$yandexAuthLoading || logoutBusy}
          onclick={() => void refreshYandexAuth()}
        >
          Обновить статус
        </button>
        {#if confirmLogout}
          <span class="confirm" role="group" aria-label="Подтверждение отключения аккаунта">
            Отключить аккаунт?
            <button
              type="button"
              class="danger"
              disabled={logoutBusy}
              onclick={() => {
                confirmLogout = false;
                void logout();
              }}
            >
              {logoutBusy ? 'Отключаем…' : 'Да, отключить'}
            </button>
            <button type="button" class="ghost" onclick={() => (confirmLogout = false)}>
              Нет
            </button>
          </span>
        {:else}
          <button
            type="button"
            class="danger"
            disabled={logoutBusy}
            onclick={() => (confirmLogout = true)}
          >
            Отключить аккаунт
          </button>
        {/if}
      </div>

      <p class="note">
        Выход удаляет токен и refresh на сервере и очищает каталог на экране. Токен
        не покидает backend: ни в REST, ни в WS его нет.
      </p>
    </section>
  {:else}
    <div class="grid">
      <section class="panel" aria-labelledby="device-title">
        <h2 id="device-title">Вход по коду</h2>
        <p class="panel-sub">
          Сервер запросит код у Яндекса, покажет QR и будет опрашивать статус —
          вручную ничего вводить не нужно, кроме кода на странице Яндекса.
        </p>

        {#if !device}
          <button
            type="button"
            class="primary"
            disabled={starting}
            onclick={startDevice}
          >
            {starting ? 'Запрашиваем код…' : 'Показать QR-код'}
          </button>
          {#if deviceError}
            <div class="notice error" role="alert">{deviceError}</div>
          {/if}
        {:else}
          <div class="device-body">
            <div class="qr">
              <img
                src={device.qr_data_uri}
                alt="QR-код для входа в Яндекс Музыку"
                width="168"
                height="168"
              />
            </div>

            <div class="device-info">
              <span class="label">Код для ввода</span>
              <code class="user-code">{device.user_code}</code>

              <span class="label">Адрес подтверждения</span>
              <a
                class="verify-link"
                href={device.verification_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                {device.verification_url}
              </a>

              <div class="status-line">
                <span
                  class="chip"
                  class:pending={deviceState === 'pending'}
                  class:ok={deviceState === 'authorized'}
                  class:bad={deviceState !== null && ['expired', 'denied', 'error'].includes(deviceState)}
                >
                  {deviceStateText}
                </span>
                {#if deviceState === 'pending' && remaining > 0}
                  <span class="countdown">осталось {formatRemaining(remaining)}</span>
                {/if}
              </div>

              {#if deviceLogin}
                <span class="login-hint">Аккаунт: {deviceLogin}</span>
              {/if}

              {#if deviceError}
                <div class="notice error" role="alert">{deviceError}</div>
              {/if}

              {#if deviceState === 'expired' || deviceState === 'denied' || deviceState === 'error'}
                <button type="button" class="primary" disabled={starting} onclick={startDevice}>
                  Начать заново
                </button>
              {/if}
            </div>
          </div>

          <p class="note">
            QR и код не секретны. Device code и токен остаются на сервере; браузер
            получает только обновление статуса.
          </p>
        {/if}
      </section>

      <section class="panel" aria-labelledby="token-title">
        <h2 id="token-title">Токен вручную</h2>
        <p class="panel-sub">
          Вставьте токен, полученный в Яндекс OAuth. Сервер проверит его и сохранит
          в защищённый файл — в браузере токен не останется.
        </p>

        <form class="token-form" onsubmit={submitToken}>
          <label class="field">
            <span>Токен Яндекса</span>
            <input
              type="password"
              autocomplete="off"
              spellcheck="false"
              placeholder="y0_..."
              bind:value={tokenValue}
              aria-label="Токен Яндекса"
            />
          </label>
          {#if tokenError}
            <span class="field-error" role="alert">{tokenError}</span>
          {/if}
          <button type="submit" class="primary" disabled={tokenBusy || !tokenValue}>
            {tokenBusy ? 'Проверяем…' : 'Сохранить токен'}
          </button>
        </form>

        <p class="note">
          Если задан <code>security.pin</code>, сервер сначала попросит PIN — он
          откроется в отдельном диалоге и повторит вход.
        </p>
      </section>
    </div>
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && closePin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="setup-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="setup-pin-title">PIN для изменения настроек</h2>
        <p>
          Вход в Яндекс Музыку — изменяющее действие и защищён PIN-кодом сервера.
          Введите PIN, вход повторится автоматически.
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
        {#if pinError}<span class="field-error" role="alert">{pinError}</span>{/if}
        <div class="modal-actions">
          <button type="button" class="ghost" onclick={closePin}>Отмена</button>
          <button type="submit" class="primary" disabled={pinBusy || !pin}>Войти</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .setup {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.6);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .setup-head {
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

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }

  .panel {
    border: 1px solid var(--border);
    background: rgba(12, 16, 28, 0.66);
    border-radius: 20px;
    padding: clamp(16px, 2vw, 22px);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  h2 {
    margin: 0;
    font-size: 1rem;
    letter-spacing: 0.01em;
  }

  .panel-sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    max-width: 60ch;
  }

  .note {
    margin: 0;
    color: var(--muted);
    font-size: 0.72rem;
    opacity: 0.9;
    max-width: 66ch;
  }

  code {
    font-family: inherit;
    color: #c4b5fd;
  }

  button.primary,
  button.ghost,
  button.danger {
    padding: 9px 16px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    align-self: flex-start;
  }

  button.primary {
    border: 0;
    color: #0b0715;
    background: var(--accent);
    box-shadow: 0 12px 26px -14px rgba(139, 92, 246, 0.8);
  }

  button.ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  button.danger {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.1);
  }

  button.danger:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.18);
  }

  .signed-in {
    gap: 14px;
  }

  .signed-head {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .badge-ok {
    align-self: flex-start;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #6ee7b7;
    border: 1px solid rgba(110, 231, 183, 0.3);
    background: rgba(16, 185, 129, 0.1);
  }

  .login-line {
    margin: 0;
    color: var(--muted);
    font-size: 0.86rem;
    overflow-wrap: anywhere;
  }

  .facts {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
  }

  .facts div {
    min-width: 180px;
  }

  dt {
    color: var(--muted);
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  dd {
    margin: 3px 0 0;
    font-size: 0.86rem;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .row-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .row-actions button {
    align-self: auto;
  }

  .device-body {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    align-items: flex-start;
  }

  .qr {
    padding: 8px;
    border-radius: 16px;
    background: #f8fafc;
    border: 1px solid rgba(255, 255, 255, 0.2);
    line-height: 0;
  }

  .qr img {
    display: block;
    width: 168px;
    height: 168px;
    border-radius: 8px;
  }

  .device-info {
    display: flex;
    flex-direction: column;
    gap: 5px;
    min-width: 0;
    flex: 1;
  }

  .label {
    color: var(--muted);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .user-code {
    font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
    font-size: 1.3rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    color: var(--text);
    user-select: all;
  }

  .verify-link {
    color: var(--accent-2);
    font-size: 0.82rem;
    overflow-wrap: anywhere;
  }

  .status-line {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 4px;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 700;
  }

  .chip.pending {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.3);
    background: rgba(252, 211, 77, 0.1);
  }

  .chip.ok {
    color: #6ee7b7;
    border-color: rgba(110, 231, 183, 0.3);
    background: rgba(16, 185, 129, 0.1);
  }

  .chip.bad {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.1);
  }

  .countdown {
    color: var(--muted);
    font-size: 0.74rem;
    font-variant-numeric: tabular-nums;
  }

  .login-hint {
    color: var(--muted);
    font-size: 0.74rem;
    overflow-wrap: anywhere;
  }

  .notice {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 13px;
    border-radius: 13px;
    font-size: 0.78rem;
    border: 1px solid var(--border);
    background: var(--surface);
  }

  .notice.error {
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
    overflow-wrap: anywhere;
  }

  .token-form {
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-items: flex-start;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    width: 100%;
    color: var(--muted);
    font-size: 0.72rem;
  }

  .field input {
    padding: 10px 12px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    font-size: 0.86rem;
    letter-spacing: 0.06em;
  }

  .field input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 1px;
  }

  .field-error {
    color: var(--danger);
    font-size: 0.76rem;
    overflow-wrap: anywhere;
  }

  .skeleton {
    padding: 20px;
    border-radius: 16px;
    border: 1px dashed var(--border);
    color: var(--muted);
    font-size: 0.82rem;
  }

  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 40;
    display: grid;
    place-items: center;
    padding: 20px;
    background: rgba(4, 6, 12, 0.66);
  }

  .modal {
    width: min(400px, 100%);
    padding: 20px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: #0d1220;
    box-shadow: 0 30px 70px -30px rgba(0, 0, 0, 0.9);
  }

  .modal h2 {
    margin-bottom: 6px;
  }

  .modal p {
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .modal input {
    width: 100%;
    padding: 10px 12px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    letter-spacing: 0.2em;
  }

  .modal input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 1px;
  }

  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
  }

  @media (max-width: 960px) {
    .grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  @media (max-width: 560px) {
    .qr img {
      width: 140px;
      height: 140px;
    }
    .user-code {
      font-size: 1.1rem;
    }
    .setup-head {
      align-items: flex-start;
    }
  }

  .confirm {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    color: #fecdd3;
    font-size: 0.78rem;
    font-weight: 600;
  }

  /* Touch confirmation buttons must clear the 44px target floor too. */
  .confirm button {
    min-height: 44px;
  }
</style>
