<script lang="ts">
  /**
   * PC-style quick controls in the top bar: Wi-Fi, Bluetooth and playback output.
   * Each icon opens a compact popover; the full pages (Network / Zones) remain the
   * place for advanced actions. The server stays the source of truth — every
   * action calls the same stores as the full panels and adopts their answer.
   */
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import Icon from '../Icon.svelte';
  import {
    connectWifi,
    disconnectWifi,
    networkBusy,
    networkError,
    networkLoading,
    networkStatus,
    refreshNetworkStatus,
    scanWifi,
    wifiNetworks,
    wifiScanError,
    wifiScanLoading,
  } from '../../lib/networkStores';
  import {
    describeNetworkError,
    networkBackendLabel,
    securityRequiresPassword,
    signalStrengthLabel,
    isMockNetworkBackend,
  } from '../../lib/network';
  import {
    activeZone,
    bluetoothBackend,
    bluetoothDevices,
    bluetoothError,
    bluetoothLoading,
    bluetoothScanning,
    requestActivateZone,
    requestBluetoothConnect,
    requestBluetoothPair,
    requestBluetoothScan,
    refreshBluetoothDevices,
    refreshZones,
    zones,
    zonesBusy,
    zonesError,
    zonesLoading,
  } from '../../lib/zonesStores';
  import {
    bluetoothBackendLabel,
    bluetoothDeviceLabel,
    bluetoothDeviceState,
    zoneKindLabel,
    zoneStateLabel,
  } from '../../lib/zones';
  import type { AudioZone, BluetoothDevice } from '../../lib/zones';

  export let onOpenNetwork: () => void = () => {};
  export let onOpenZones: () => void = () => {};
  export let onToast: (message: string, tone?: 'info' | 'error') => void = () => {};

  type Panel = 'wifi' | 'bt' | 'output' | null;

  let open: Panel = null;
  let root: HTMLElement | null = null;
  let wifiError = '';
  let connectingSsid: string | null = null;
  let password = '';
  let wifiBusy = false;
  let btError = '';
  let btBusy = false;

  $: wifiConnected = $networkStatus?.connected === true;
  $: wired =
    wifiConnected && Boolean($networkStatus?.interface?.toLowerCase().startsWith('eth'));
  $: wifiLabel = wired
    ? `Ethernet: ${$networkStatus?.ip ?? 'подключено'}`
    : wifiConnected
      ? ($networkStatus?.ssid ?? 'Wi-Fi')
      : 'Wi-Fi не подключён';
  $: btConnected = $bluetoothDevices.find((device) => device.connected) ?? null;
  $: btLabel = btConnected ? bluetoothDeviceLabel(btConnected) : 'Bluetooth';
  $: outputLabel = $activeZone?.name ?? 'Вывод звука';
  $: outputIcon =
    $activeZone?.kind === 'browser'
      ? 'monitor'
      : $activeZone?.kind === 'bt'
        ? 'bluetooth'
        : 'speaker';

  onMount(() => {
    // Populate the tray without opening anything; these are cheap cached reads.
    void refreshNetworkStatus();
    void refreshBluetoothDevices();
    void refreshZones();
  });

  function toggle(panel: Exclude<Panel, null>): void {
    open = open === panel ? null : panel;
    wifiError = '';
    btError = '';
    if (open === 'wifi') void refreshNetworkStatus();
    if (open === 'bt') {
      void refreshBluetoothDevices();
      void refreshZones();
    }
    if (open === 'output') void refreshZones();
  }

  function close(): void {
    open = null;
    connectingSsid = null;
    password = '';
  }

  function closeOnOutside(event: PointerEvent): void {
    if (open === null || root === null) return;
    if (!root.contains(event.target as Node)) close();
  }

  function closeOnEscape(event: KeyboardEvent): void {
    if (event.key === 'Escape') close();
  }

  function reducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function panelIn(node: HTMLElement) {
    return fly(node, { y: -5, duration: reducedMotion() ? 0 : 140, easing: cubicOut });
  }

  function goNetwork(): void {
    close();
    onOpenNetwork();
  }

  function goZones(): void {
    close();
    onOpenZones();
  }

  async function scan(): Promise<void> {
    wifiError = '';
    try {
      await scanWifi();
    } catch (error) {
      wifiError = describeNetworkError(error);
    }
  }

  function chooseNetwork(ssid: string, security: string | null): void {
    wifiError = '';
    if (securityRequiresPassword(security)) {
      connectingSsid = ssid;
      password = '';
      return;
    }
    void connect(ssid, '');
  }

  async function submitConnect(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!connectingSsid) return;
    await connect(connectingSsid, password);
  }

  async function connect(ssid: string, secret: string): Promise<void> {
    wifiBusy = true;
    wifiError = '';
    try {
      await connectWifi(ssid, secret);
      connectingSsid = null;
      password = '';
      onToast(`Wi-Fi: подключено к «${ssid}».`);
      await refreshNetworkStatus();
    } catch (error) {
      wifiError = describeNetworkError(error);
    } finally {
      wifiBusy = false;
    }
  }

  async function disconnect(): Promise<void> {
    wifiBusy = true;
    wifiError = '';
    try {
      await disconnectWifi();
      onToast('Wi-Fi отключён.');
    } catch (error) {
      wifiError = describeNetworkError(error);
    } finally {
      wifiBusy = false;
    }
  }

  async function scanBluetooth(): Promise<void> {
    btError = '';
    try {
      await requestBluetoothScan();
      onToast('Bluetooth: сканирование запущено.');
    } catch (error) {
      btError = error instanceof Error ? error.message : 'Не удалось запустить сканирование.';
    }
  }

  async function connectBluetooth(device: BluetoothDevice): Promise<void> {
    btBusy = true;
    btError = '';
    try {
      if (!device.paired) {
        await requestBluetoothPair(device.address);
      }
      await requestBluetoothConnect(device.address);
      onToast(`Bluetooth: «${bluetoothDeviceLabel(device)}» подключено.`);
    } catch (error) {
      btError = error instanceof Error ? error.message : 'Не удалось подключить устройство.';
    } finally {
      btBusy = false;
    }
  }

  async function activate(zone: AudioZone): Promise<void> {
    if (zone.active || !zone.available) return;
    try {
      await requestActivateZone(zone.id);
      onToast(`Вывод звука: «${zone.name}».`);
      close();
    } catch (error) {
      onToast(error instanceof Error ? error.message : 'Не удалось переключить вывод.', 'error');
    }
  }

  function zoneIcon(kind: AudioZone['kind']): string {
    if (kind === 'bt') return 'bluetooth';
    if (kind === 'browser') return 'monitor';
    return 'speaker';
  }
</script>

<svelte:window onpointerdown={closeOnOutside} onkeydown={closeOnEscape} />

<div class="quick" bind:this={root}>
  <div class="tray" role="group" aria-label="Быстрое управление">
    <button
      type="button"
      class="tray-button"
      class:on={wifiConnected}
      title={wifiLabel}
      aria-label={`Wi-Fi: ${wifiLabel}`}
      aria-expanded={open === 'wifi'}
      aria-controls="quick-wifi"
      onclick={() => toggle('wifi')}
    >
      <Icon name={wired ? 'ethernet' : wifiConnected ? 'wifi' : 'wifi-off'} size={18} />
      <span class="dot" class:live={wifiConnected} aria-hidden="true"></span>
    </button>

    <button
      type="button"
      class="tray-button"
      class:on={btConnected !== null}
      title={btLabel}
      aria-label={`Bluetooth: ${btLabel}`}
      aria-expanded={open === 'bt'}
      aria-controls="quick-bt"
      onclick={() => toggle('bt')}
    >
      <Icon name="bluetooth" size={18} />
      <span class="dot" class:live={btConnected !== null} aria-hidden="true"></span>
    </button>

    <button
      type="button"
      class="tray-button"
      class:on={true}
      title={`Вывод звука: ${outputLabel}`}
      aria-label={`Вывод звука: ${outputLabel}`}
      aria-expanded={open === 'output'}
      aria-controls="quick-output"
      onclick={() => toggle('output')}
    >
      <Icon name={outputIcon} size={18} />
    </button>
  </div>

  {#if open === 'wifi'}
    <div class="panel material-overlay" id="quick-wifi" role="dialog" aria-label="Wi-Fi" in:panelIn>
      <header class="panel-head">
        <span class="panel-title"><Icon name={wired ? 'ethernet' : 'wifi'} size={16} /> Сеть</span>
        {#if $networkStatus}
          <span class="chip">{networkBackendLabel($networkStatus.backend)}</span>
        {/if}
      </header>

      {#if $networkLoading}
        <p class="muted">Читаем состояние сети…</p>
      {:else if $networkStatus}
        {#if $networkStatus.connected}
          <div class="network-row">
            <Icon name={wired ? 'ethernet' : 'wifi'} size={18} />
            <div class="network-main">
              <strong>{$networkStatus.ssid ?? (wired ? 'Ethernet' : 'Сеть')}</strong>
              <span>
                {$networkStatus.ip ?? 'IP не сообщён'}
                {#if $networkStatus.signal !== null}· {signalStrengthLabel($networkStatus.signal)}{/if}
                {#if $networkStatus.internet === false}· без интернета{/if}
              </span>
            </div>
            <button type="button" class="ghost" disabled={wifiBusy} onclick={disconnect}>
              Отключить
            </button>
          </div>
        {:else}
          <p class="muted">
            Подключение отсутствует.
            {#if isMockNetworkBackend($networkStatus.backend)} Сеть — mock-адаптер.{/if}
          </p>
        {/if}
      {:else if $networkError}
        <p class="error">{$networkError}</p>
      {/if}

      <div class="panel-actions">
        <button type="button" class="ghost" disabled={$wifiScanLoading || wifiBusy} onclick={scan}>
          {$wifiScanLoading ? 'Сканируем…' : 'Сканировать'}
        </button>
      </div>

      {#if wifiError}<p class="error">{wifiError}</p>{/if}
      {#if $wifiScanError}<p class="error">{$wifiScanError}</p>{/if}

      {#if connectingSsid}
        <form class="connect-form" onsubmit={submitConnect}>
          <label for="quick-wifi-password">Пароль для «{connectingSsid}»</label>
          <input
            id="quick-wifi-password"
            type="password"
            autocomplete="current-password"
            bind:value={password}
            maxlength="128"
          />
          <div class="form-actions">
            <button
              type="button"
              class="ghost"
              onclick={() => {
                connectingSsid = null;
                password = '';
              }}
            >
              Отмена
            </button>
            <button type="submit" class="primary" disabled={wifiBusy || !password}>
              {wifiBusy ? 'Подключаем…' : 'Подключить'}
            </button>
          </div>
        </form>
      {:else if $wifiNetworks.length > 0}
        <ul class="network-list">
          {#each $wifiNetworks as network (network.ssid)}
            <li>
              <button
                type="button"
                class="network-item"
                class:current={network.connected}
                disabled={network.connected || wifiBusy || $networkBusy !== null}
                onclick={() => chooseNetwork(network.ssid, network.security)}
              >
                <Icon name={network.security ? 'lock' : 'wifi'} size={15} />
                <span class="network-name">{network.ssid}</span>
                <span class="network-meta">{signalStrengthLabel(network.signal)}</span>
                {#if network.connected}<Icon name="check" size={15} />{/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <footer class="panel-foot">
        <button type="button" class="link" onclick={goNetwork}>Настроить сеть</button>
      </footer>
    </div>
  {/if}

  {#if open === 'bt'}
    <div class="panel material-overlay" id="quick-bt" role="dialog" aria-label="Bluetooth" in:panelIn>
      <header class="panel-head">
        <span class="panel-title"><Icon name="bluetooth" size={16} /> Bluetooth</span>
        {#if $bluetoothBackend}
          <span class="chip">{bluetoothBackendLabel($bluetoothBackend)}</span>
        {/if}
      </header>

      {#if $bluetoothLoading}
        <p class="muted">Читаем устройства…</p>
      {:else if $bluetoothDevices.length === 0}
        <p class="muted">Устройств нет. Запустите сканирование и включите pairing на колонке.</p>
      {:else}
        <ul class="device-list">
          {#each $bluetoothDevices as device (device.address)}
            <li class="device">
              <Icon name="bluetooth" size={17} />
              <div class="device-main">
                <strong>{bluetoothDeviceLabel(device)}</strong>
                <span>{bluetoothDeviceState(device)} · {device.address}</span>
              </div>
              {#if device.connected}
                <span class="state-ok" title="Подключено" aria-label="Подключено">
                  <Icon name="check" size={15} />
                </span>
              {:else}
                <button
                  type="button"
                  class="ghost small"
                  disabled={btBusy}
                  onclick={() => connectBluetooth(device)}
                >
                  {btBusy ? '…' : device.paired ? 'Подключить' : 'Сопрячь'}
                </button>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}

      {#if btError}<p class="error">{btError}</p>{/if}
      {#if $bluetoothError}<p class="error">{$bluetoothError}</p>{/if}

      <div class="panel-actions">
        <button type="button" class="ghost" disabled={$bluetoothScanning} onclick={scanBluetooth}>
          {$bluetoothScanning ? 'Сканируем…' : 'Сканировать'}
        </button>
      </div>

      <footer class="panel-foot">
        <button type="button" class="link" onclick={goZones}>Зоны и Bluetooth</button>
      </footer>
    </div>
  {/if}

  {#if open === 'output'}
    <div class="panel material-overlay" id="quick-output" role="dialog" aria-label="Вывод звука" in:panelIn>
      <header class="panel-head">
        <span class="panel-title"><Icon name="speaker" size={16} /> Вывод звука</span>
        {#if $activeZone}<span class="chip">{zoneStateLabel($activeZone)}</span>{/if}
      </header>

      {#if $zonesLoading && $zones.length === 0}
        <p class="muted">Читаем зоны…</p>
      {:else if $zonesError}
        <p class="error">{$zonesError}</p>
      {:else}
        <ul class="zone-list">
          {#each $zones as zone (zone.id)}
            <li>
              <button
                type="button"
                class="zone-item"
                class:active={zone.active}
                disabled={!zone.available || $zonesBusy !== null}
                onclick={() => activate(zone)}
              >
                <Icon name={zoneIcon(zone.kind)} size={17} />
                <span class="zone-main">
                  <strong>{zone.name}</strong>
                  <span>{zoneKindLabel(zone.kind)} · {zoneStateLabel(zone)}</span>
                </span>
                {#if zone.active}<Icon name="check" size={16} />{/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <footer class="panel-foot">
        <button type="button" class="link" onclick={goZones}>Настроить зоны</button>
      </footer>
    </div>
  {/if}
</div>

<style>
  .quick {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .tray {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: rgba(255, 255, 255, 0.035);
  }

  .tray-button {
    position: relative;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 999px;
    color: var(--text-muted);
    transition:
      color var(--motion-fast) ease,
      background var(--motion-fast) ease;
  }

  .tray-button:hover,
  .tray-button[aria-expanded='true'] {
    color: var(--text);
    background: var(--surface-hover);
  }

  .tray-button.on {
    color: var(--accent-2);
  }

  .dot {
    position: absolute;
    right: 5px;
    bottom: 4px;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0;
  }

  .dot.live {
    opacity: 0.9;
  }

  .panel {
    position: absolute;
    z-index: 40;
    top: calc(100% + 9px);
    right: 0;
    width: min(360px, calc(100vw - 24px));
    max-height: min(70vh, 520px);
    overflow: auto;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    text-align: left;
  }

  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .panel-title {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--text);
    font-size: 0.86rem;
    font-weight: 700;
  }

  .chip {
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.64rem;
    font-weight: 700;
  }

  .muted {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.45;
  }

  .error {
    margin: 0;
    color: #fecdd3;
    font-size: 0.74rem;
    line-height: 1.4;
  }

  .network-row {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 9px 10px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.03);
  }

  .network-main {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    flex: 1;
  }

  .network-main strong,
  .device-main strong {
    color: var(--text);
    font-size: 0.8rem;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .network-main span,
  .device-main span {
    color: var(--muted);
    font-size: 0.68rem;
    overflow-wrap: anywhere;
  }

  .panel-actions {
    display: flex;
    gap: 6px;
  }

  .ghost {
    padding: 7px 11px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.72rem;
    font-weight: 700;
  }

  .ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .ghost.small {
    padding: 5px 9px;
    font-size: 0.68rem;
  }

  .primary {
    padding: 7px 12px;
    border-radius: 999px;
    border: 0;
    background: var(--accent);
    color: #0b0715;
    font-size: 0.72rem;
    font-weight: 700;
  }

  .network-list,
  .device-list,
  .zone-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .network-item,
  .zone-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 9px;
    border-radius: 10px;
    border: 1px solid transparent;
    color: var(--text-soft);
    text-align: left;
    font-size: 0.76rem;
  }

  .network-item:hover:not(:disabled),
  .zone-item:hover:not(:disabled) {
    background: var(--surface-hover);
    border-color: var(--border);
  }

  .network-item.current,
  .zone-item.active {
    color: var(--text);
    border-color: color-mix(in srgb, var(--accent-2) 28%, transparent);
    background: color-mix(in srgb, var(--accent-2) 8%, transparent);
  }

  .network-item:disabled,
  .zone-item:disabled {
    opacity: 0.55;
  }

  .network-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .network-meta {
    color: var(--muted);
    font-size: 0.66rem;
    font-variant-numeric: tabular-nums;
  }

  .device {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 8px;
    border-radius: 10px;
    border: 1px solid transparent;
  }

  .device:hover {
    border-color: var(--border);
    background: rgba(255, 255, 255, 0.02);
  }

  .device-main {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    flex: 1;
  }

  .state-ok {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    color: #6ee7b7;
    font-size: 0.66rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .zone-main {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    flex: 1;
  }

  .zone-main strong {
    font-size: 0.8rem;
    font-weight: 600;
  }

  .zone-main span {
    color: var(--muted);
    font-size: 0.66rem;
  }

  .connect-form {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: 10px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.03);
  }

  .connect-form label {
    color: var(--text-soft);
    font-size: 0.72rem;
    font-weight: 600;
  }

  .connect-form input {
    width: 100%;
    padding: 8px 10px;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    font-size: 0.82rem;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
  }

  .panel-foot {
    display: flex;
    justify-content: flex-end;
    padding-top: 2px;
    border-top: 1px solid var(--border);
  }

  .link {
    padding: 4px 0;
    color: var(--accent-2);
    font-size: 0.72rem;
    font-weight: 700;
  }

  .link:hover {
    text-decoration: underline;
  }

  @media (max-width: 900px) {
    .panel {
      right: -6px;
    }
  }

  @media (pointer: coarse) {
    .tray-button {
      width: 44px;
      height: 44px;
    }
  }
</style>
