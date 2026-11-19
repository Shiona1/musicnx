<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    NetworkPinCancelledError,
    PASSWORD_MAX_LENGTH,
    SSID_MAX_LENGTH,
    describeNetworkError,
    isMockNetworkBackend,
    isRetryableNetworkPinError,
    networkBackendLabel,
    normalizeSsid,
    passwordError,
    securityRequiresPassword,
    signalLevel,
    signalStrengthLabel,
    ssidError,
    wifiSecurityLabel,
    wifiSignalText,
    withNetworkPinRetry,
  } from '../lib/network';
  import type { NetworkPinPrompt, NetworkStatus, WifiNetwork } from '../lib/network';
  import {
    connectWifi,
    disconnectWifi,
    networkBusy,
    networkError,
    networkLoading,
    networkRouteMissing,
    networkStatus,
    networkUpdatedAt,
    refreshNetworkStatus,
    scanWifi,
    wifiNetworks,
    wifiNetworksTotal,
    wifiScanBackend,
    wifiScanError,
    wifiScanLoading,
    wifiScanRouteMissing,
    wifiScanUpdatedAt,
  } from '../lib/networkStores';
  import type { NetworkBusyAction } from '../lib/networkStores';

  /**
   * P8 Network / Wi-Fi panel — docs/API.md §13, docs/UI.md (CardsNX).
   *
   * Everything shown is server-owned: `GET /network/status` is read on mount and
   * on demand; the scan / connect / disconnect answers replace the status
   * directly. There is no network WebSocket event, so nothing is fabricated
   * between reads. `POST /network/scan|connect|disconnect` are PIN/localhost
   * protected: the built-in dialog (or the injected `onPinRequired` hook) asks
   * for a PIN, `POST /auth/pin` opens the HttpOnly session cookie and the exact
   * request is replayed once. Neither the PIN nor the Wi-Fi password is stored.
   *
   * Honesty rules (AGENTS.md §12): a `mock` backend is labelled as virtual; a
   * backend without P8 routes answers 404 and the panel says so instead of
   * drawing fake network cards. AP-onboarding, PIN change and TLS are listed as
   * «не реализовано» — as text, never as buttons.
   */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: NetworkPinPrompt | null = null;

  const MAX_PIN_ROUNDS = 3;

  let ssidText = '';
  /** Kept only while the user is editing; cleared as soon as connect settles. */
  let password = '';
  let ssidTouched = false;
  let passwordTouched = false;
  let actionMessage = '';
  let actionKind: 'ok' | 'error' = 'ok';
  let liveStatus = '';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let ssidInput: HTMLInputElement | null = null;
  let passwordInput: HTMLInputElement | null = null;
  /** Single-flight latch for the explicit buttons (PIN dialog included). */
  let actionRunning = false;

  let status: NetworkStatus | null = null;
  let busy: NetworkBusyAction | null = null;
  let backend: string | null = null;
  let selectedNetwork: WifiNetwork | null = null;
  let ssidProblem: string | null = null;
  let passwordProblem: string | null = null;
  let controlsBlocked = false;
  let canSubmit = false;
  let mockBackend = false;
  let connected = false;
  let scanStale = false;

  $: status = $networkStatus;
  $: busy = $networkBusy;
  $: controlsBlocked = actionRunning || busy !== null;
  $: backend = status?.backend ?? $wifiScanBackend;
  $: mockBackend = isMockNetworkBackend(backend);
  $: connected = status?.connected === true;
  $: selectedNetwork = findNetwork($wifiNetworks, ssidText);
  $: ssidProblem = ssidTouched ? ssidError(ssidText) : null;
  $: passwordProblem = passwordError(password, selectedNetwork?.security ?? null);
  $: canSubmit =
    !controlsBlocked &&
    ssidError(ssidText) === null &&
    passwordError(password, selectedNetwork?.security ?? null) === null;
  // The status is re-read after any mutation; the list stays the last scan.
  $: scanStale = $wifiScanUpdatedAt > 0 && $networkUpdatedAt > $wifiScanUpdatedAt;

  onMount(() => {
    void refreshNetworkStatus();
  });

  onDestroy(() => {
    // A dialog unmounted mid-action must not leave the promise hanging.
    const resolve = pinResolve;
    pinResolve = null;
    if (resolve) resolve(null);
  });

  function findNetwork(items: WifiNetwork[], text: string): WifiNetwork | null {
    const target = normalizeSsid(text).toLowerCase();
    if (target === '') return null;
    return items.find((item) => item.ssid.toLowerCase() === target) ?? null;
  }

  function clock(ms: number): string {
    return new Date(ms).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  function notify(text: string, kind: 'ok' | 'error' = 'ok'): void {
    liveStatus = text;
    if (onToast) {
      actionMessage = '';
      onToast(text, kind === 'error' ? 'error' : 'info');
      return;
    }
    actionMessage = text;
    actionKind = kind;
  }

  async function withAction(operation: () => Promise<void>): Promise<void> {
    if (actionRunning || busy !== null) return;
    actionRunning = true;
    try {
      await operation();
    } finally {
      actionRunning = false;
    }
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

  type Outcome = { ok: true } | { ok: false; message: string; cancelled: boolean };

  /** Replays a mutation after PIN login; keeps the dialog open on a wrong PIN. */
  async function mutateWithPin(operation: () => Promise<void>): Promise<Outcome> {
    for (let round = 0; round < MAX_PIN_ROUNDS; round += 1) {
      try {
        await withNetworkPinRetry(operation, askPin);
        return { ok: true };
      } catch (error) {
        if (error instanceof NetworkPinCancelledError) {
          return { ok: false, message: describeNetworkError(error), cancelled: true };
        }
        if (isRetryableNetworkPinError(error) && !onPinRequired) {
          pinError = describeNetworkError(error);
          continue;
        }
        return { ok: false, message: describeNetworkError(error), cancelled: false };
      }
    }
    return { ok: false, message: 'PIN не принят: действие с сетью не выполнено.', cancelled: false };
  }

  function refresh(): void {
    if ($networkLoading || controlsBlocked) return;
    void refreshNetworkStatus();
  }

  function scanNetworks(): void {
    if (actionRunning || busy !== null) return;
    pinError = '';
    actionMessage = '';
    void withAction(async () => {
      const outcome = await mutateWithPin(async () => {
        await scanWifi();
      });
      if (!outcome.ok) {
        // A real scan failure is already shown inside the scan card; a cancelled
        // PIN prompt is an action outcome and is announced here.
        if (outcome.cancelled || $wifiScanError === null) notify(outcome.message, 'error');
        return;
      }
      notify(`Сканирование выполнено: сетей — ${$wifiNetworks.length}.`);
    });
  }

  function pickNetwork(network: WifiNetwork): void {
    if (controlsBlocked) return;
    ssidText = network.ssid;
    // Never carry a secret from one network to another.
    password = '';
    ssidTouched = false;
    passwordTouched = false;
    actionMessage = '';
    liveStatus = `Выбрана сеть «${network.ssid}». Введите пароль и подтвердите подключение.`;
    void tick().then(() => passwordInput?.focus());
  }

  function clearForm(): void {
    ssidText = '';
    password = '';
    ssidTouched = false;
    passwordTouched = false;
    void tick().then(() => ssidInput?.focus());
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (actionRunning || busy !== null) return;
    ssidTouched = true;
    passwordTouched = true;

    const problem = ssidError(ssidText);
    if (problem !== null) {
      notify(problem, 'error');
      return;
    }
    const known = findNetwork($wifiNetworks, ssidText);
    const secretProblem = passwordError(password, known?.security ?? null);
    if (secretProblem !== null) {
      notify(secretProblem, 'error');
      return;
    }

    const ssid = normalizeSsid(ssidText);
    // The secret is captured for the request and deliberately not stored.
    const secret = password;
    pinError = '';
    actionMessage = '';
    void withAction(async () => {
      const outcome = await mutateWithPin(async () => {
        await connectWifi(ssid, secret);
      });
      // Clear the field as soon as the attempt settles: a cancelled or failed
      // request must not leave the Wi-Fi password sitting in the DOM.
      password = '';
      passwordTouched = false;
      if (!outcome.ok) {
        notify(outcome.message, 'error');
        return;
      }
      ssidTouched = false;
      notify(`Запрос на подключение к «${ssid}» выполнен: сервер вернул состояние сети.`);
    });
  }

  function disconnectNetwork(): void {
    if (actionRunning || busy !== null) return;
    const ssid = status?.ssid ?? null;
    pinError = '';
    actionMessage = '';
    void withAction(async () => {
      const outcome = await mutateWithPin(async () => {
        await disconnectWifi();
      });
      password = '';
      passwordTouched = false;
      if (!outcome.ok) {
        notify(outcome.message, 'error');
        return;
      }
      notify(
        ssid === null
          ? 'Wi-Fi отключён: сервер вернул новое состояние сети.'
          : `Wi-Fi «${ssid}» отключён: сервер вернул новое состояние сети.`,
      );
    });
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section
  class="network-panel material-content"
  aria-labelledby="network-title"
  aria-busy={$networkLoading || controlsBlocked}
>
  <header class="network-head">
    <div class="head-text">
      <span class="eyebrow">Система</span>
      <h1 id="network-title">Сеть и Wi-Fi</h1>
      <p class="sub">
        Состояние Wi-Fi берётся с сервера: подключение, SSID, IP, сигнал и наличие
        интернета. Список сетей отдаёт NetworkManager; интерфейс ничего не
        придумывает и не хранит пароль. Локальная медиатека работает и без интернета.
      </p>
    </div>
    <div class="head-actions">
      {#if backend}
        <span
          class="chip"
          class:mock={mockBackend}
          title="Адаптер сети, который ответил на запрос"
        >
          backend: {networkBackendLabel(backend)}
        </span>
      {/if}
      {#if $networkUpdatedAt > 0}
        <span class="updated" title="Последнее успешное чтение GET /network/status">
          обновлено {clock($networkUpdatedAt)}
        </span>
      {/if}
      <button
        type="button"
        class="ghost"
        disabled={$networkLoading || controlsBlocked}
        onclick={refresh}
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
  {:else if onToast && liveStatus}
    <span class="sr-only" role="status" aria-live="polite">{liveStatus}</span>
  {/if}

  {#if $networkRouteMissing}
    <div class="notice error" role="alert">
      <strong>Сервер не поддерживает управление сетью.</strong>
      <span>
        <code>GET /api/v1/network/status</code> вернул 404: в этой сборке backend
        нет маршрутов P8. Wi-Fi настраивается на самой плате; панель не выдумывает
        состояние и не показывает неработающих кнопок.
      </span>
      <button type="button" class="ghost small" disabled={$networkLoading} onclick={refresh}>
        Повторить
      </button>
    </div>
  {:else if $networkLoading && status === null}
    <div class="nx-cards" aria-hidden="true">
      {#each [0, 1, 2] as key (key)}
        <div class="nx-card skeleton-card">
          <span class="skeleton-line short"></span>
          <span class="skeleton-line"></span>
          <span class="skeleton-line"></span>
        </div>
      {/each}
    </div>
    <span class="sr-only" role="status">Загружаем состояние сети…</span>
  {:else if status === null}
    <div class="notice error" role="alert">
      <span>{$networkError ?? 'Статус сети не получен.'}</span>
      <button type="button" class="ghost small" disabled={$networkLoading} onclick={refresh}>
        Повторить
      </button>
    </div>
  {:else}
    {#if $networkError}
      <div class="notice error" role="alert">
        <span>{$networkError}</span>
        <button type="button" class="ghost small" disabled={$networkLoading || controlsBlocked} onclick={refresh}>
          Повторить
        </button>
      </div>
    {/if}

    {#if mockBackend}
      <div class="notice info">
        <strong>Mock-сеть.</strong>
        <span>
          Сервер использует mock-адаптер сети: SSID, IP, сигнал и список сетей
          виртуальные, реальный Wi-Fi платы не опрашивается и не переключается.
          Подключение меняет только состояние сервера.
        </span>
      </div>
    {/if}

    <div class="nx-cards">
      <section class="nx-card" aria-labelledby="network-status-title">
        <div class="card-head">
          <h2 id="network-status-title">Состояние</h2>
          <span class="status" class:on={connected} class:off={!connected}>
            {connected ? 'Подключено' : 'Не подключено'}
          </span>
        </div>

        <dl class="facts">
          <div class="fact">
            <dt>Backend</dt>
            <dd>{networkBackendLabel(status.backend)}</dd>
          </div>
          <div class="fact">
            <dt>Интерфейс</dt>
            <dd>{status.interface ?? '—'}</dd>
          </div>
          <div class="fact">
            <dt>SSID</dt>
            <dd>{status.ssid ?? '—'}</dd>
          </div>
          <div class="fact">
            <dt>IP-адрес</dt>
            <dd>{status.ip ?? '—'}</dd>
          </div>
          <div class="fact">
            <dt>Сигнал</dt>
            <dd>
              {wifiSignalText(status.signal)}
              <span class="fact-note">
                {status.signal === null ? 'сервер не сообщил' : signalStrengthLabel(status.signal)}
              </span>
            </dd>
          </div>
          <div class="fact">
            <dt>Интернет</dt>
            <dd>
              {status.internet === null
                ? 'сервер не сообщил'
                : status.internet
                  ? 'есть'
                  : 'нет'}
            </dd>
          </div>
        </dl>

        {#if status.message}
          <p class="server-message">Сообщение сервера: {status.message}</p>
        {/if}

        <div class="status-actions">
          {#if connected}
            <button
              type="button"
              class="ghost danger"
              disabled={controlsBlocked}
              onclick={disconnectNetwork}
            >
              {busy === 'disconnect' ? 'Отключаем…' : 'Отключиться'}
            </button>
          {:else}
            <span class="hint">
              Wi-Fi не подключён: выберите сеть в списке или введите SSID вручную.
              Ethernet работает независимо от Wi-Fi.
            </span>
          {/if}
        </div>
      </section>

      <form
        class="nx-card"
        aria-labelledby="network-connect-title"
        onsubmit={(event) => void submitConnect(event)}
        novalidate
      >
        <h2 id="network-connect-title">Подключение к Wi-Fi</h2>
        <p class="card-sub">
          Введите SSID и пароль. Пароль уходит только в запросе подключения: он не
          сохраняется в браузере, не пишется в логи и очищается из поля сразу после
          ответа сервера.
        </p>

        <label class="field-label" for="network-ssid">Имя сети (SSID)</label>
        <input
          id="network-ssid"
          class="text-input"
          type="text"
          maxlength={SSID_MAX_LENGTH}
          bind:this={ssidInput}
          bind:value={ssidText}
          autocomplete="off"
          spellcheck={false}
          disabled={controlsBlocked}
          aria-describedby="network-ssid-hint"
          aria-invalid={ssidProblem !== null}
          onblur={() => (ssidTouched = true)}
        />
        {#if ssidProblem}
          <p class="field-error" role="alert">{ssidProblem}</p>
        {/if}
        <p id="network-ssid-hint" class="hint">
          До {SSID_MAX_LENGTH} символов; регистр важен. Скрытую сеть можно ввести
          вручную, если её нет в списке.
        </p>

        <label class="field-label" for="network-password">Пароль</label>
        <input
          id="network-password"
          class="text-input"
          type="password"
          maxlength={PASSWORD_MAX_LENGTH}
          bind:this={passwordInput}
          bind:value={password}
          autocomplete="new-password"
          autocapitalize="off"
          autocorrect="off"
          spellcheck={false}
          disabled={controlsBlocked}
          aria-describedby="network-password-hint"
          aria-invalid={passwordTouched && passwordProblem !== null}
          onblur={() => (passwordTouched = true)}
        />
        {#if passwordTouched && passwordProblem}
          <p class="field-error" role="alert">{passwordProblem}</p>
        {/if}
        <p id="network-password-hint" class="hint">
          {#if selectedNetwork && securityRequiresPassword(selectedNetwork.security)}
            Сеть «{selectedNetwork.ssid}» защищена
            ({wifiSecurityLabel(selectedNetwork.security)}) — пароль обязателен.
          {:else if selectedNetwork}
            Сеть «{selectedNetwork.ssid}» открытая — оставьте пароль пустым.
          {:else}
            Пароль нужен только защищённой сети; для открытой оставьте поле пустым.
          {/if}
          Пароль не сохраняется и не логируется; сервер его не возвращает.
        </p>

        <div class="form-actions">
          <button type="submit" class="primary" disabled={!canSubmit}>
            {busy === 'connect' ? 'Подключаемся…' : 'Подключиться'}
          </button>
          {#if ssidText || password}
            <button type="button" class="ghost" disabled={controlsBlocked} onclick={clearForm}>
              Очистить
            </button>
          {/if}
        </div>
      </form>

      <section class="nx-card" aria-labelledby="network-boundaries-title">
        <h2 id="network-boundaries-title">Границы</h2>
        <ul class="boundary-list">
          <li>
            <strong>Точка доступа (AP-onboarding)</strong> — не реализовано:
            интерфейс не поднимает AP и не показывает QR/пароль для первичного
            подключения к плате.
          </li>
          <li>
            <strong>TLS/HTTPS</strong> — не реализовано: сертификаты настраиваются
            вне веб-интерфейса.
          </li>
          <li>
            <strong>Failover Ethernet/Wi-Fi</strong> — штатное поведение
            NetworkManager (Ethernet приоритетнее); панель только читает
            фактическое состояние.
          </li>
        </ul>
        <p class="hint">
          Пункты без API показаны текстом, а не кнопками: фиктивных действий нет.
        </p>
      </section>
    </div>

    <section class="nx-card" aria-labelledby="network-scan-title" aria-busy={$wifiScanLoading}>
      <div class="card-head">
        <h2 id="network-scan-title">Доступные сети</h2>
        <div class="card-actions">
          {#if $wifiScanUpdatedAt > 0}
            <span class="updated">сканировано {clock($wifiScanUpdatedAt)}</span>
          {/if}
          <button
            type="button"
            class="ghost small"
            disabled={controlsBlocked || $networkLoading}
            onclick={scanNetworks}
          >
            {$wifiScanLoading ? 'Сканируем…' : 'Сканировать'}
          </button>
        </div>
      </div>
      <p class="card-sub">
        Список отдаёт сервер: SSID, сигнал и защита; пароли в нём не возвращаются.
        Сканирование — изменяющий запрос.
      </p>

      {#if $wifiScanRouteMissing}
        <div class="notice error" role="alert">
          <span>
            <code>POST /api/v1/network/scan</code> вернул 404: сканирование не
            реализовано в этой сборке backend. Список не выдумывается, пока сервер
            его не подтвердит.
          </span>
          <button
            type="button"
            class="ghost small"
            disabled={controlsBlocked}
            onclick={scanNetworks}
          >
            Повторить
          </button>
        </div>
      {:else if $wifiScanError}
        <div class="notice error" role="alert">
          <span>{$wifiScanError}</span>
          <button
            type="button"
            class="ghost small"
            disabled={controlsBlocked}
            onclick={scanNetworks}
          >
            Повторить
          </button>
        </div>
      {:else if $wifiScanLoading && $wifiNetworks.length === 0}
        <ul class="networks" aria-hidden="true">
          {#each [0, 1, 2] as row (row)}
            <li class="network skeleton-row">
              <span class="skeleton-line"></span>
            </li>
          {/each}
        </ul>
        <span class="sr-only" role="status">Сканируем сети…</span>
      {:else if $wifiNetworks.length === 0}
        <div class="empty">
          <strong>Список сетей пуст</strong>
          <p>
            Сервер ещё не сканировал эфир. Нажмите «Сканировать». Скрытую сеть
            можно подключить через форму выше.
          </p>
        </div>
      {:else}
        <ul class="networks">
          {#each $wifiNetworks as network (network.ssid)}
            <li class="network" class:connected={network.connected}>
              <span class="meter" aria-hidden="true">
                {#each [1, 2, 3, 4] as bar}
                  <span class="bar" class:on={signalLevel(network.signal) >= bar}></span>
                {/each}
              </span>
              <span class="network-main">
                <span class="network-ssid" title={network.ssid}>{network.ssid}</span>
                <span class="network-meta">
                  {wifiSecurityLabel(network.security)} · сигнал
                  {wifiSignalText(network.signal)} ({signalStrengthLabel(network.signal)})
                </span>
              </span>
              {#if network.connected}
                <span class="status on">Подключено</span>
              {:else}
                <button
                  type="button"
                  class="ghost small"
                  disabled={controlsBlocked}
                  aria-label={`Выбрать сеть «${network.ssid}» для подключения`}
                  onclick={() => pickNetwork(network)}
                >
                  Выбрать
                </button>
              {/if}
            </li>
          {/each}
        </ul>
        {#if $wifiNetworksTotal > $wifiNetworks.length}
          <p class="hint">показано {$wifiNetworks.length} из {$wifiNetworksTotal}</p>
        {/if}
        {#if scanStale}
          <p class="hint">
            Список получен раньше последнего изменения статуса — при необходимости
            отсканируйте снова.
          </p>
        {/if}
      {/if}
    </section>
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="network-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="network-pin-title">PIN для управления сетью</h2>
        <p>
          Сканирование, подключение и отключение — изменяющие действия. Введите
          PIN: последний запрос повторится автоматически. PIN не сохраняется в
          браузере.
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
  .network-panel {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }

  .network-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 18px;
    flex-wrap: wrap;
  }

  .head-text {
    min-width: 0;
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
    line-height: 1.5;
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
    padding: 5px 11px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
    max-width: 280px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chip.mock {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.28);
    background: rgba(139, 92, 246, 0.08);
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

  .notice.info {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.24);
    background: rgba(139, 92, 246, 0.07);
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
    flex: 1 1 260px;
  }

  .notice code {
    font-family: inherit;
    font-size: 0.74rem;
    color: #c4b5fd;
  }

  .nx-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
    align-items: stretch;
  }

  /* CardsNX: dark glass cards, soft border, calm micro-transitions (docs/UI.md §7). */
  .nx-card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
    margin: 0;
    padding: 16px;
    border-radius: 18px;
    border: 1px solid var(--border);
    background: linear-gradient(160deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.02));
    box-shadow: 0 18px 40px -30px rgba(0, 0, 0, 0.9);
    transition: border-color 160ms ease, background 160ms ease;
  }

  .nx-card h2 {
    margin: 0;
    font-size: 0.96rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }

  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }

  .card-sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.76rem;
    line-height: 1.5;
    max-width: 68ch;
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-left: auto;
  }

  .facts {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .fact {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    padding: 8px 10px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.42);
  }

  .fact dt {
    color: var(--muted);
    font-size: 0.64rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .fact dd {
    margin: 0;
    font-size: 0.84rem;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .fact-note {
    display: block;
    color: var(--muted);
    font-size: 0.7rem;
    line-height: 1.45;
  }

  .server-message {
    margin: 0;
    padding: 8px 10px;
    border-radius: 12px;
    border: 1px dashed var(--border);
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
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

  .status.on {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.1);
  }

  .status.off {
    color: #fca5a5;
    border-color: rgba(248, 113, 113, 0.28);
    background: rgba(248, 113, 113, 0.07);
  }

  .status-actions {
    margin-top: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .field-label {
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
  }

  .text-input {
    width: 100%;
    padding: 9px 11px;
    border-radius: 11px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    font-size: 0.86rem;
  }

  .text-input:disabled {
    opacity: 0.6;
  }

  .field-error {
    margin: 0;
    color: #fca5a5;
    font-size: 0.74rem;
    line-height: 1.4;
  }

  .hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .form-actions {
    margin-top: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .primary {
    padding: 9px 18px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    color: #0b0715;
    border: 0;
    background: var(--accent);
    box-shadow: 0 12px 26px -16px rgba(139, 92, 246, 0.85);
  }

  .primary:hover:not(:disabled) {
    filter: brightness(1.06);
  }

  .networks {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .network {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    padding: 9px 11px;
    border-radius: 14px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.02);
    transition: background 140ms ease, border-color 140ms ease;
  }

  .network:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--border);
  }

  .network.connected {
    border-color: rgba(34, 211, 238, 0.2);
    background: rgba(34, 211, 238, 0.045);
  }

  .network-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .network-ssid {
    font-size: 0.82rem;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .network-meta {
    color: var(--muted);
    font-size: 0.68rem;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .meter {
    display: inline-flex;
    align-items: flex-end;
    gap: 2px;
    width: 22px;
    height: 18px;
    flex: none;
  }

  .bar {
    width: 3px;
    border-radius: 2px;
    background: rgba(255, 255, 255, 0.14);
  }

  .bar:nth-child(1) {
    height: 25%;
  }

  .bar:nth-child(2) {
    height: 45%;
  }

  .bar:nth-child(3) {
    height: 70%;
  }

  .bar:nth-child(4) {
    height: 100%;
  }

  .bar.on {
    background: linear-gradient(180deg, var(--accent-2), var(--accent));
  }

  .empty {
    padding: 18px;
    border-radius: 16px;
    border: 1px dashed var(--border);
    background: rgba(10, 14, 24, 0.35);
    color: var(--muted);
  }

  .empty strong {
    display: block;
    margin-bottom: 4px;
    color: var(--text);
    font-size: 0.9rem;
  }

  .empty p {
    margin: 0;
    max-width: 70ch;
    font-size: 0.76rem;
    line-height: 1.5;
  }

  .boundary-list {
    margin: 0;
    padding: 0 0 0 18px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    color: var(--muted);
    font-size: 0.73rem;
    line-height: 1.5;
  }

  .boundary-list strong {
    color: var(--text);
  }

  .skeleton-card {
    min-height: 132px;
    justify-content: center;
  }

  .skeleton-row {
    display: block;
  }

  .skeleton-line {
    display: block;
    width: 100%;
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
    border: 0;
    background: var(--accent);
  }

  @media (max-width: 900px) {
    .network-head {
      align-items: flex-start;
    }
    .head-actions {
      width: 100%;
    }
  }

  @media (max-width: 760px) {
    .nx-cards {
      grid-template-columns: 1fr;
    }
    .network {
      grid-template-columns: auto minmax(0, 1fr);
    }
    .network > .status,
    .network > button {
      grid-column: 2;
      justify-self: start;
    }
    .form-actions .primary,
    .form-actions .ghost {
      flex: 1 1 auto;
      min-height: 40px;
    }
    .card-actions {
      width: 100%;
      margin-left: 0;
    }
  }

  @media (max-width: 480px) {
    .network-panel {
      padding: 16px;
      border-radius: 20px;
    }
    .form-actions .primary,
    .form-actions .ghost {
      width: 100%;
      min-height: 44px;
    }
    .network-panel .ghost {
      min-height: 40px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .network,
    .nx-card,
    .skeleton-line {
      transition: none;
      animation: none;
    }
  }
</style>
