<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount } from 'svelte';
  import {
    ZONE_OFFSET_LIMIT_MS,
    ZONE_OFFSET_STEP_MS,
    ZONE_VOLUME_MAX,
    ZONE_VOLUME_MIN,
    ZonesPinCancelledError,
    bluetoothBackendLabel,
    bluetoothDeviceLabel,
    bluetoothDeviceState,
    clampZoneVolume,
    describeZonesError,
    isMockBluetoothBackend,
    isRetryableZonesPinError,
    stepZoneOffset,
    withZonesPinRetry,
    zoneKindHint,
    zoneKindLabel,
    zoneOffsetText,
    zoneStateLabel,
    zoneVolumeText,
  } from '../lib/zones';
  import type { AudioZone, BluetoothDevice, ZonesPinPrompt } from '../lib/zones';
  import {
    activeZone,
    bluetoothBackend,
    bluetoothDevices,
    bluetoothDevicesTotal,
    bluetoothError,
    bluetoothLoading,
    bluetoothRouteMissing,
    bluetoothScanning,
    requestActivateZone,
    requestBluetoothConnect,
    requestBluetoothForget,
    requestBluetoothPair,
    requestBluetoothScan,
    requestZoneOffset,
    requestZoneVolume,
    refreshBluetoothDevices,
    refreshZonesSection,
    startZonesPolling,
    stopBluetoothScan,
    zones,
    zonesBusy,
    zonesError,
    zonesLoading,
    zonesRouteMissing,
    zonesUpdatedAt,
  } from '../lib/zonesStores';
  import type { ZonesBusyAction } from '../lib/zonesStores';

  /**
   * P7 Zones / Quick Panel — audio outputs of the Pi and Bluetooth devices
   * (docs/API.md §11, docs/UI.md §3.5).
   *
   * Every displayed value is server-owned: `GET /zones` and
   * `GET /bluetooth/devices` are read on mount and re-read after each action;
   * `zones.updated` / `bt.device_*` events feed the stores. Activate, volume,
   * offset and the Bluetooth mutations are PIN/localhost protected — the PIN is
   * requested by the built-in dialog (or the injected `onPinRequired` hook),
   * sent to `POST /auth/pin`, and the exact request is replayed. The PIN is
   * never stored and no token ever reaches the browser.
   *
   * Honesty rules: a backend without P7 answers `404` and the panel says so
   * instead of drawing fake zones (AGENTS.md §12); a `mock` Bluetooth backend
   * is labelled "без железа"; volume/offset the server did not send render as
   * "—" and their controls are disabled, never as an invented number.
   */
  export let mode: string | null = null;
  /** External toast hook; null renders the message inline in the panel. */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: ZonesPinPrompt | null = null;
  /** `page` = full section (own heading); `panel` = compact Quick Panel. */
  export let variant: 'page' | 'panel' = 'page';
  /** Only used by `panel`: renders a close button when provided. */
  export let onClose: (() => void) | null = null;

  const MAX_PIN_ROUNDS = 3;

  let zonesList: AudioZone[] = [];
  let devicesList: BluetoothDevice[] = [];
  let active: AudioZone | null = null;
  let busy: { action: ZonesBusyAction; targetId: string | null } | null = null;

  let actionMessage = '';
  let actionKind: 'ok' | 'error' = 'ok';
  let actionRunning = false;
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let forgetAddress: string | null = null;
  /** Local slider positions while a gesture is in flight (server is truth). */
  let volumeDrafts: Record<string, number> = {};

  $: zonesList = $zones;
  $: devicesList = $bluetoothDevices;
  $: active = $activeZone;
  $: busy = $zonesBusy;
  $: mockMode = mode === 'mock';
  $: bluetoothIsMock = mockMode || isMockBluetoothBackend($bluetoothBackend);
  $: scanDisabled = $bluetoothRouteMissing || (busy !== null && busy.action !== 'scan');
  $: forgetPending = forgetAddress !== null;
  $: emptyZones =
    !$zonesLoading && zonesList.length === 0 && !$zonesRouteMissing && !$zonesError;
  $: emptyDevices =
    !$bluetoothLoading && devicesList.length === 0 && !$bluetoothRouteMissing;
  $: activeName = active?.name ?? null;

  onMount(() => {
    const stopPolling = startZonesPolling();
    return () => {
      stopPolling();
      stopBluetoothScan();
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
    if (event.key === 'Escape' && forgetAddress !== null) forgetAddress = null;
  }

  type Outcome = { ok: true } | { ok: false; message: string };

  /** Replays a mutation after PIN login; keeps the dialog open on a wrong PIN. */
  async function mutateWithPin(operation: () => Promise<void>): Promise<Outcome> {
    for (let round = 0; round < MAX_PIN_ROUNDS; round += 1) {
      try {
        await withZonesPinRetry(operation, askPin);
        return { ok: true };
      } catch (error) {
        if (error instanceof ZonesPinCancelledError) {
          return { ok: false, message: describeZonesError(error) };
        }
        if (isRetryableZonesPinError(error) && !onPinRequired) {
          pinError = describeZonesError(error);
          continue;
        }
        return { ok: false, message: describeZonesError(error) };
      }
    }
    return { ok: false, message: 'PIN не принят: действие не выполнено.' };
  }

  async function perform(operation: () => Promise<void>, okMessage: string): Promise<void> {
    actionMessage = '';
    pinError = '';
    const outcome = await mutateWithPin(operation);
    if (!outcome.ok) {
      notify(outcome.message, 'error');
      return;
    }
    notify(okMessage);
  }

  /** Single-flight wrapper for the explicit buttons (slider writes bypass it). */
  function startAction(operation: () => Promise<void>, okMessage: string): void {
    if (actionRunning || busy !== null) return;
    actionRunning = true;
    void (async () => {
      try {
        await perform(operation, okMessage);
      } finally {
        actionRunning = false;
      }
    })();
  }

  function refreshAll(): void {
    if ($zonesLoading) return;
    void refreshZonesSection();
  }

  function activateZone(zone: AudioZone): void {
    if (zone.active || !zone.available) return;
    startAction(
      async () => {
        await requestActivateZone(zone.id);
      },
      `Активная зона: «${zone.name}». Трек не прерывается — сервер переносит вывод.`,
    );
  }

  /** True while another control would race this zone's `action`. */
  function blockedFor(
    state: { action: ZonesBusyAction; targetId: string | null } | null,
    zoneId: string,
    action: ZonesBusyAction,
  ): boolean {
    if (state === null) return false;
    return !(state.targetId === zoneId && state.action === action);
  }

  function dropVolumeDraft(zoneId: string): void {
    if (!(zoneId in volumeDrafts)) return;
    const next = { ...volumeDrafts };
    delete next[zoneId];
    volumeDrafts = next;
  }

  function fieldValue(event: Event): number {
    const target = event.currentTarget;
    if (target instanceof HTMLInputElement) return Number(target.value);
    return 0;
  }

  function onVolumeInput(zoneId: string, value: number): void {
    if (!Number.isFinite(value)) return;
    volumeDrafts = { ...volumeDrafts, [zoneId]: clampZoneVolume(value) };
  }

  function commitVolumeNow(zone: AudioZone, value: number): void {
    const requested = clampZoneVolume(value);
    volumeDrafts = { ...volumeDrafts, [zone.id]: requested };
    void commitVolume(zone.id, requested);
  }

  async function commitVolume(zoneId: string, value: number): Promise<void> {
    const requested = clampZoneVolume(value);
    const outcome = await mutateWithPin(async () => {
      await requestZoneVolume(zoneId, requested);
    });
    if (!outcome.ok) {
      // The server rejected the value: snap the slider back to the truth.
      dropVolumeDraft(zoneId);
      notify(outcome.message, 'error');
      return;
    }
    if (volumeDrafts[zoneId] === requested) dropVolumeDraft(zoneId);
  }

  function shiftOffset(zone: AudioZone, deltaMs: number): void {
    if (zone.offset_ms === null || !zone.available) return;
    const next = stepZoneOffset(zone.offset_ms, deltaMs);
    if (next === zone.offset_ms) return;
    startAction(
      async () => {
        await requestZoneOffset(zone.id, next);
      },
      `Смещение зоны «${zone.name}»: ${zoneOffsetText(next)}.`,
    );
  }

  function scanDevices(): void {
    if ($bluetoothScanning) {
      stopBluetoothScan();
      return;
    }
    startAction(
      async () => {
        await requestBluetoothScan();
      },
      'Сканирование запущено: список обновляется автоматически.',
    );
  }

  function pairDevice(device: BluetoothDevice): void {
    startAction(
      async () => {
        await requestBluetoothPair(device.address);
      },
      `Сопряжение с «${bluetoothDeviceLabel(device)}» запрошено.`,
    );
  }

  function connectDevice(device: BluetoothDevice): void {
    startAction(
      async () => {
        await requestBluetoothConnect(device.address);
      },
      `Подключение к «${bluetoothDeviceLabel(device)}» запрошено.`,
    );
  }

  function confirmForget(device: BluetoothDevice): void {
    forgetAddress = null;
    startAction(
      async () => {
        await requestBluetoothForget(device.address);
      },
      `Сопряжение с «${bluetoothDeviceLabel(device)}» удалено.`,
    );
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }

  function clock(ms: number): string {
    return new Date(ms).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section
  class="zones material-content"
  class:panel={variant === 'panel'}
  aria-label="Зоны вывода и Bluetooth"
>
  <header class="zones-head">
    <div class="head-text">
      <span class="eyebrow">Аудио</span>
      {#if variant === 'panel'}
        <h2>Зоны и Bluetooth</h2>
      {:else}
        <h1>Зоны и Bluetooth</h1>
      {/if}
      <p class="sub">
        Выходы Pi: jack 3,5 мм, USB-DAC, Bluetooth A2DP и вкладка браузера.
        Переключение переносит текущий трек без остановки; громкость и смещение
        живут на сервере. Если backend ещё не знает маршрутов P7, панель скажет
        об этом вместо выдуманного списка.
      </p>
    </div>
    <div class="head-actions">
      {#if activeName}
        <span class="chip playing" title="Зона, в которую сервер выводит звук сейчас">
          <span class="dot" aria-hidden="true"></span>
          {activeName}
        </span>
      {/if}
      {#if $bluetoothBackend}
        <span class="chip bt" title="адаптер Bluetooth на сервере">
          BT: {bluetoothBackendLabel($bluetoothBackend)}
        </span>
      {/if}
      {#if $zonesUpdatedAt > 0}
        <span class="updated" title="Последнее успешное чтение GET /zones">
          обновлено {clock($zonesUpdatedAt)}
        </span>
      {/if}
      <button type="button" class="ghost" disabled={$zonesLoading} onclick={refreshAll}>
        Обновить
      </button>
      {#if variant === 'panel' && onClose}
        <button
          type="button"
          class="ghost icon"
          aria-label="Закрыть панель зон"
          onclick={() => onClose?.()}
        >
          <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true">
            <path d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z" />
          </svg>
        </button>
      {/if}
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

  {#if mockMode}
    <div class="notice info">
      <strong>Mock-режим.</strong>
      <span>
        Зоны и Bluetooth виртуальные: настоящие jack/USB-DAC и BlueZ не
        опрашиваются, переключение меняет только состояние сервера.
      </span>
    </div>
  {/if}

  {#if $zonesRouteMissing}
    <div class="notice error" role="alert">
      <strong>Сервер не поддерживает зоны.</strong>
      <span>
        <code>GET /api/v1/zones</code> вернул 404: в этой сборке backend нет
        маршрутов P7. Список не выдумывается, кнопки управления не сработают —
        включите P7 backend и повторите.
      </span>
      <button type="button" class="ghost small" onclick={refreshAll}>Повторить</button>
    </div>
  {:else if $zonesError && !showPin}
    <div class="notice error" role="alert">
      <span>{$zonesError}</span>
      <button type="button" class="ghost small" onclick={refreshAll}>Повторить</button>
    </div>
  {/if}

  <span class="sr-only" role="status" aria-live="polite">
    {activeName ? `Активная зона: ${activeName}.` : 'Активная зона не сообщена сервером.'}
  </span>

  {#if !$zonesRouteMissing}
    {#if $zonesLoading && zonesList.length === 0}
      <div class="cards" aria-hidden="true">
        {#each [0, 1, 2] as key (key)}
          <div class="card skeleton-card">
            <span class="skeleton-line short"></span>
            <span class="skeleton-line"></span>
            <span class="skeleton-line"></span>
          </div>
        {/each}
      </div>
      <span class="sr-only" role="status">Загружаем список зон…</span>
    {:else if emptyZones}
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor" aria-hidden="true">
          <path
            d="M4 9v6h4l5 4V5L8 9H4zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"
          />
        </svg>
        <div>
          <strong>Сервер не вернул ни одной зоны</strong>
          <p>
            <code>GET /zones</code> ответил успешно, но список пуст. Проверьте
            адаптер аудио на сервере (jack/USB-DAC должны появиться в
            <code>pactl list short sinks</code>) и обновите панель.
          </p>
        </div>
      </div>
    {:else}
      <div class="cards">
        {#each zonesList as zone (zone.id)}
          <article
            class="card zone"
            class:active={zone.active}
            class:unavailable={!zone.available}
            aria-label={`Зона «${zone.name}»`}
          >
            <div class="card-head">
              <div class="zone-title">
                <h3 title={zone.name}>{zone.name}</h3>
                <span class="kind">{zoneKindLabel(zone.kind)}</span>
                {#if zoneKindHint(zone.kind)}
                  <span class="kind-hint">{zoneKindHint(zone.kind)}</span>
                {/if}
              </div>
              <span class="status" class:on={zone.active} class:off={!zone.available}>
                {zoneStateLabel(zone)}
              </span>
            </div>

            {#if zone.sink_name}
              <p class="sink" title="Sink PipeWire/ALSA на сервере">
                <code>{zone.sink_name}</code>
              </p>
            {/if}

            <div class="control">
              <div class="control-head">
                <span class="label">Громкость</span>
                <span class="value">
                  {zoneVolumeText(volumeDrafts[zone.id] ?? zone.volume)}
                  {#if busy?.action === 'volume' && busy.targetId === zone.id}
                    <span class="saving">· сохраняем…</span>
                  {/if}
                </span>
              </div>
              <input
                type="range"
                min={ZONE_VOLUME_MIN}
                max={ZONE_VOLUME_MAX}
                step="1"
                value={volumeDrafts[zone.id] ?? zone.volume ?? ZONE_VOLUME_MIN}
                disabled={zone.volume === null ||
                  !zone.available ||
                  blockedFor(busy, zone.id, 'volume')}
                aria-label={`Громкость зоны «${zone.name}», проценты`}
                aria-valuetext={zoneVolumeText(volumeDrafts[zone.id] ?? zone.volume)}
                title={zone.volume === null
                  ? 'Сервер не прислал громкость этой зоны'
                  : 'Громкость этой зоны на сервере (0–100)'}
                oninput={(event) => onVolumeInput(zone.id, fieldValue(event))}
                onchange={(event) => commitVolumeNow(zone, fieldValue(event))}
              />
              {#if zone.volume === null}
                <p class="hint">Сервер не прислал громкость — управление недоступно.</p>
              {/if}
            </div>

            <div class="control">
              <div class="control-head">
                <span class="label">Смещение</span>
                <span class="value">{zoneOffsetText(zone.offset_ms)}</span>
              </div>
              <div class="offset-row">
                <button
                  type="button"
                  class="step"
                  aria-label={`Уменьшить смещение зоны «${zone.name}» на ${ZONE_OFFSET_STEP_MS} мс`}
                  title={`−${ZONE_OFFSET_STEP_MS} мс`}
                  disabled={zone.offset_ms === null ||
                    !zone.available ||
                    blockedFor(busy, zone.id, 'offset')}
                  onclick={() => shiftOffset(zone, -ZONE_OFFSET_STEP_MS)}
                >
                  −
                </button>
                <span class="offset-hint" title={`Допустимо ±${ZONE_OFFSET_LIMIT_MS} мс`}>
                  шаг {ZONE_OFFSET_STEP_MS} мс
                </span>
                <button
                  type="button"
                  class="step"
                  aria-label={`Увеличить смещение зоны «${zone.name}» на ${ZONE_OFFSET_STEP_MS} мс`}
                  title={`+${ZONE_OFFSET_STEP_MS} мс`}
                  disabled={zone.offset_ms === null ||
                    !zone.available ||
                    blockedFor(busy, zone.id, 'offset')}
                  onclick={() => shiftOffset(zone, ZONE_OFFSET_STEP_MS)}
                >
                  +
                </button>
              </div>
            </div>

            <div class="zone-actions">
              {#if zone.active && !zone.available}
                <span class="active-note muted">
                  Активна, но sink исчез — звука нет. Выберите другую зону.
                </span>
              {:else if zone.active}
                <span class="active-note">Сюда играет mpv сейчас</span>
              {:else if !zone.available}
                <span class="active-note muted">
                  Устройство недоступно: sink исчез или отключён на сервере.
                </span>
              {:else}
                <button
                  type="button"
                  class="primary"
                  disabled={busy !== null}
                  title="Переключить вывод на эту зону — текущий трек не прервётся"
                  aria-label={`Сделать зону «${zone.name}» активной`}
                  onclick={() => activateZone(zone)}
                >
                  {busy?.action === 'activate' && busy.targetId === zone.id
                    ? 'Переключаем…'
                    : 'Сделать активной'}
                </button>
              {/if}
            </div>
          </article>
        {/each}
      </div>
      <p class="hint zones-hint">
        Смещение — задержка зоны (±{ZONE_OFFSET_LIMIT_MS} мс, шаг
        {ZONE_OFFSET_STEP_MS} мс): её учитывает караоке, звук при этом не
        пересинхронизируется.
      </p>
    {/if}
  {/if}

  <div class="group">
    <div class="group-head">
      <h2>Bluetooth</h2>
      <span class="group-count">
        {devicesList.length}{#if $bluetoothDevicesTotal > devicesList.length}
          из {$bluetoothDevicesTotal}
        {/if}
      </span>
      {#if $bluetoothBackend}
        <span class="chip bt" title="backend Bluetooth на сервере">
          {bluetoothBackendLabel($bluetoothBackend)}
        </span>
      {/if}
      <div class="group-actions">
        <button
          type="button"
          class="ghost small"
          disabled={scanDisabled}
          title="Запросить сканирование BlueZ (PIN или localhost)"
          onclick={scanDevices}
        >
          {$bluetoothScanning ? 'Остановить обновление' : 'Сканировать'}
        </button>
        <button
          type="button"
          class="ghost small"
          disabled={$bluetoothLoading}
          onclick={() => void refreshBluetoothDevices()}
        >
          Обновить список
        </button>
      </div>
    </div>

    {#if bluetoothIsMock}
      <div class="notice info">
        <span>
          адаптер Bluetooth — <strong>mock</strong>: устройства виртуальные,
          реальное радио BlueZ не опрашивается. Сопряжение/подключение меняет
          только состояние сервера.
        </span>
      </div>
    {/if}

    {#if $bluetoothScanning}
      <p class="scanning" role="status" aria-live="polite">
        <span class="pulse" aria-hidden="true"></span>
        Сканирование запрошено: список перечитывается с сервера до 8 секунд
        (или до события <code>bt.device_*</code>).
      </p>
    {/if}

    {#if $bluetoothRouteMissing}
      <div class="notice error" role="alert">
        <strong>Сервер не отдаёт Bluetooth-устройства.</strong>
        <span>
          <code>GET /api/v1/bluetooth/devices</code> вернул 404: маршрутов P7 в
          этой сборке backend нет. Bluetooth-подсказки не показываются, пока
          сервер их не подтвердит.
        </span>
      </div>
    {:else if $bluetoothError}
      <div class="notice error" role="alert">
        <span>{$bluetoothError}</span>
        <button
          type="button"
          class="ghost small"
          onclick={() => void refreshBluetoothDevices()}
        >
          Повторить
        </button>
      </div>
    {/if}

    {#if $bluetoothLoading && devicesList.length === 0}
      <div class="devices" aria-hidden="true">
        {#each [0, 1] as key (key)}
          <div class="device skeleton-row">
            <span class="skeleton-line"></span>
          </div>
        {/each}
      </div>
    {:else if emptyDevices}
      <p class="group-empty">
        Устройств не найдено. Нажмите «Сканировать»: сервер запустит поиск
        BlueZ, а список обновится сам. Для сопряжения нужен PIN (или localhost).
      </p>
    {:else if devicesList.length > 0}
      <ul class="devices">
        {#each devicesList as device (device.address)}
          <li class="device" class:connected={device.connected}>
            <div class="device-main">
              <span class="device-name" title={device.name ?? device.address}>
                {bluetoothDeviceLabel(device)}
              </span>
              {#if device.name}
                <span class="device-address">{device.address}</span>
              {:else}
                <span class="device-address">имя не сообщено</span>
              {/if}
            </div>
            <span class="status" class:on={device.connected}>
              {bluetoothDeviceState(device)}
            </span>
            <div class="device-actions">
              {#if !device.paired}
                <button
                  type="button"
                  class="ghost small"
                  disabled={busy !== null}
                  title="Сопрячь устройство (PIN или localhost)"
                  onclick={() => pairDevice(device)}
                >
                  {busy?.action === 'pair' && busy.targetId === device.address
                    ? 'Сопрягаем…'
                    : 'Сопрячь'}
                </button>
              {:else if !device.connected}
                <button
                  type="button"
                  class="ghost small"
                  disabled={busy !== null}
                  title="Подключить A2DP-устройство (PIN или localhost)"
                  onclick={() => connectDevice(device)}
                >
                  {busy?.action === 'connect' && busy.targetId === device.address
                    ? 'Подключаем…'
                    : 'Подключить'}
                </button>
              {/if}
              {#if device.paired}
                {#if forgetAddress === device.address}
                  <span class="confirm" role="group" aria-label="Подтверждение удаления сопряжения">
                    Забыть?
                    <button
                      type="button"
                      class="ghost small danger"
                      disabled={busy !== null}
                      onclick={() => confirmForget(device)}
                    >
                      Да
                    </button>
                    <button
                      type="button"
                      class="ghost small"
                      onclick={() => (forgetAddress = null)}
                    >
                      Нет
                    </button>
                  </span>
                {:else}
                  <button
                    type="button"
                    class="ghost small danger"
                    disabled={busy !== null || forgetPending}
                    title="Удалить сопряжение (PIN или localhost)"
                    onclick={() => (forgetAddress = device.address)}
                  >
                    Забыть
                  </button>
                {/if}
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    <p class="hint group-hint">
      A2DP-sink регистрируется на сервере как отдельная зона. Сопряжённые
      устройства сервер переподключает сам при появлении. Bluetooth и Wi-Fi
      2,4 ГГц делят радио: для стабильного звука рекомендуется Ethernet.
    </p>
  </div>

  <p class="hint footprint">
    Состояние зон хранится на сервере: браузер не пишет токены и не использует
    localStorage, после перезагрузки панель читает всё заново.
  </p>
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="zones-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="zones-pin-title">PIN для управления зонами</h2>
        <p>
          Переключение зоны, громкость, смещение и Bluetooth — изменяющие
          действия. Введите PIN: последний запрос повторится автоматически. PIN
          не сохраняется в браузере.
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
  .zones {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }

  .zones.panel {
    border-radius: 20px;
    padding: 16px;
    gap: 12px;
  }

  .zones-head {
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

  h1,
  h2 {
    margin: 2px 0 4px;
    letter-spacing: -0.015em;
  }

  h1 {
    font-size: clamp(1.5rem, 2.6vw, 2rem);
  }

  .panel h2 {
    font-size: 1.15rem;
  }

  .sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    max-width: 78ch;
  }

  .panel .sub {
    font-size: 0.74rem;
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
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chip.playing {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.09);
  }

  .chip.bt {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.28);
    background: rgba(139, 92, 246, 0.08);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0.75;
    flex: none;
  }

  .chip.playing .dot {
    animation: blink 1.8s ease-in-out infinite;
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

  button.ghost.icon {
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    padding: 0;
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

  .notice span {
    overflow-wrap: anywhere;
    flex: 1 1 220px;
  }

  .notice code,
  .scanning code {
    font-family: inherit;
    font-size: 0.74rem;
    color: #c4b5fd;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(268px, 1fr));
    gap: 12px;
    align-items: stretch;
  }

  .panel .cards {
    grid-template-columns: 1fr;
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

  .card.zone.active {
    border-color: rgba(34, 211, 238, 0.3);
    background: linear-gradient(160deg, rgba(34, 211, 238, 0.09), rgba(139, 92, 246, 0.05));
  }

  .card.zone.unavailable {
    opacity: 0.72;
  }

  .card-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }

  .zone-title {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 8px;
    min-width: 0;
  }

  .card h3 {
    margin: 0;
    font-size: 0.96rem;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }

  .kind {
    padding: 1px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.62rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .kind-hint {
    flex-basis: 100%;
    color: var(--muted);
    font-size: 0.66rem;
  }

  .sink {
    margin: 0;
  }

  .sink code {
    font-family: inherit;
    font-size: 0.68rem;
    color: var(--muted);
    overflow-wrap: anywhere;
  }

  .control {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .control-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }

  .label {
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
  }

  .value {
    color: var(--text);
    font-size: 0.78rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .saving {
    color: var(--muted);
    font-weight: 500;
  }

  input[type='range'] {
    width: 100%;
    height: 26px;
    accent-color: var(--accent-2);
    cursor: pointer;
    background: transparent;
  }

  input[type='range']:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .offset-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .step {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 11px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 1.05rem;
    font-weight: 700;
    line-height: 1;
  }

  .step:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .offset-hint {
    color: var(--muted);
    font-size: 0.68rem;
    font-variant-numeric: tabular-nums;
  }

  .hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.7rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .hint.zones-hint {
    padding-top: 2px;
  }

  .zone-actions {
    margin-top: auto;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .active-note {
    color: #a5f3fc;
    font-size: 0.72rem;
    font-weight: 600;
  }

  .active-note.muted {
    color: var(--muted);
    font-weight: 500;
  }

  .primary {
    padding: 9px 16px;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 700;
    color: #0b0715;
    background: var(--accent);
  }

  .primary:hover:not(:disabled) {
    filter: brightness(1.06);
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

  .group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .group-head {
    display: flex;
    align-items: center;
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

  .group-actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-left: auto;
  }

  .group-empty {
    margin: 0;
    padding: 12px 4px;
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .group-hint {
    padding-top: 4px;
  }

  .scanning {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    color: #a5f3fc;
    font-size: 0.74rem;
  }

  .pulse {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
    animation: blink 1.4s ease-in-out infinite;
    flex: none;
  }

  .devices {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .device {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 14px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.02);
    transition:
      background 140ms ease,
      border-color 140ms ease;
  }

  .device:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--border);
  }

  .device.connected {
    border-color: rgba(34, 211, 238, 0.2);
    background: rgba(34, 211, 238, 0.045);
  }

  .device-main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .device-name {
    font-size: 0.82rem;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .device-address {
    color: var(--muted);
    font-size: 0.66rem;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.02em;
    overflow-wrap: anywhere;
  }

  .device-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .confirm {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #fecdd3;
    font-size: 0.7rem;
    font-weight: 600;
  }

  .hint.footprint {
    padding-top: 2px;
    font-size: 0.68rem;
    opacity: 0.85;
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
    min-height: 150px;
    justify-content: center;
  }

  .skeleton-row {
    display: flex;
    padding: 12px;
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
    background: var(--accent);
  }

  .modal-actions .primary:disabled {
    opacity: 0.45;
  }

  @media (max-width: 900px) {
    .zones-head {
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
    .device {
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .device-actions {
      justify-content: flex-start;
    }
    .group-actions {
      width: 100%;
      margin-left: 0;
    }
    .group-actions button {
      flex: 1 1 auto;
      min-height: 40px;
    }
  }

  @media (max-width: 480px) {
    .zones {
      padding: 16px;
      border-radius: 20px;
    }
    .zone-actions .primary,
    .step {
      min-height: 44px;
    }
    .primary {
      width: 100%;
      min-height: 44px;
    }
    .empty {
      flex-direction: column;
      align-items: flex-start;
    }
    .group-actions {
      flex-direction: column;
    }
    .group-actions button {
      width: 100%;
    }
  }
</style>
