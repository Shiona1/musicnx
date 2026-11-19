<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount } from 'svelte';
  import {
    describeSettingsError,
    draftFromSnapshot,
    formatBackupSize,
    formatDateTime,
    formatSettingValue,
    isRetryableSettingsPinError,
    parseVolume,
    planSettingsSave,
    settingDescription,
    settingLabel,
    SettingsPinCancelledError,
    SYNC_ENABLED_KEY,
    VOLUME_KEY,
    VOLUME_MAX,
    VOLUME_MIN,
    volumeError,
    withSettingsPinRetry,
  } from '../lib/settings';
  import type { SettingValue, SettingsPinPrompt, SettingsSnapshot } from '../lib/settings';
  import {
    authInfo,
    authInfoError,
    backups,
    backupsTotal,
    refreshAllSettings,
    refreshAuthInfo,
    requestBackup,
    requestMaintenance,
    saveSettings,
    serviceBusy,
    serviceError,
    serviceLoading,
    serviceRouteMissing,
    settingsError,
    settingsLoading,
    settingsRouteMissing,
    settingsSaving,
    settingsSnapshot,
    settingsUpdatedAt,
  } from '../lib/settingsStores';

  /**
   * P8 Settings panel — settings (docs/API.md §12) plus the «Сервис» section.
   *
   * Every value on screen is server-owned: `GET /settings` returns
   * `{values, editable}` and only keys listed in `editable` get a control; the
   * rest are shown read-only. Saving sends a partial `PUT /settings` with the
   * changed whitelisted keys only (`player.default_volume` 0..100,
   * `sync.enabled`), PIN/localhost protected: the built-in dialog (or the
   * injected `onPinRequired` hook) supplies a PIN, `POST /auth/pin` opens the
   * HttpOnly session cookie and the exact request is replayed once. The PIN is
   * never stored anywhere.
   *
   * A backend without P8 answers `404`: the panel says so and hides the form
   * instead of rendering fake switches (AGENTS.md §12). The «Сервис» block only
   * appears after `GET /system/backups` really answered — the list is read from
   * the server and the backup/maintenance buttons are only offered once that
   * probe passed. Wi-Fi/AP, PIN changes and OTA are listed as «не реализовано»,
   * never as dead buttons.
   */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;
  /** External PIN dialog hook; null renders the built-in one. */
  export let onPinRequired: SettingsPinPrompt | null = null;

  const MAX_PIN_ROUNDS = 3;

  interface ReadOnlyRow {
    key: string;
    label: string;
    value: string;
    note: string | null;
  }

  let volumeText = '';
  let syncEnabled = false;
  /** Needed when the server reports no current value: only a user flip is sent. */
  let syncTouched = false;
  let touched = false;
  /** A cached store snapshot must fill the draft before the first paint. */
  let draftInitialized = false;
  let actionMessage = '';
  let actionKind: 'ok' | 'error' = 'ok';
  let liveStatus = '';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let currentPin = '';
  let newPin = '';
  let confirmPin = '';
  let pinChangeBusy = false;
  let pinChangeError = '';
  let pinChangeOk = '';

  async function changePin(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    pinChangeError = '';
    pinChangeOk = '';
    if (newPin.length < 4) {
      pinChangeError = 'Новый PIN должен быть не короче 4 символов.';
      return;
    }
    if (newPin !== confirmPin) {
      pinChangeError = 'Новый PIN и подтверждение не совпадают.';
      return;
    }
    pinChangeBusy = true;
    try {
      const response = await fetch('/api/v1/auth/pin/change', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_pin: currentPin, new_pin: newPin }),
      });
      if (!response.ok) {
        pinChangeError = response.status === 401
          ? 'Текущий PIN неверен. PIN не изменён.'
          : 'Не удалось изменить PIN. PIN не изменён.';
        return;
      }
      currentPin = '';
      newPin = '';
      confirmPin = '';
      pinChangeOk = 'PIN изменён. Новый PIN хранится только как scrypt-хэш.';
      onToast?.('PIN изменён');
      await refreshAuthInfo();
    } catch {
      pinChangeError = 'Сервер не ответил: PIN не изменён.';
    } finally {
      pinChangeBusy = false;
    }
  }

  $: snapshot = $settingsSnapshot;
  $: editable = snapshot?.editable ?? [];
  $: volumeEditable = editable.includes(VOLUME_KEY);
  $: syncEditable = editable.includes(SYNC_ENABLED_KEY);
  $: canEditAny = volumeEditable || syncEditable;

  $: serverVolumeRaw = snapshot === null ? undefined : snapshot.values[VOLUME_KEY];
  $: serverVolume = typeof serverVolumeRaw === 'number' ? serverVolumeRaw : null;
  $: serverVolumeText = serverVolume === null ? 'сервер не прислал значение' : `${serverVolume} %`;
  $: serverSyncRaw = snapshot === null ? undefined : snapshot.values[SYNC_ENABLED_KEY];
  $: serverSync = typeof serverSyncRaw === 'boolean' ? serverSyncRaw : null;
  $: serverSyncText =
    serverSync === null ? 'сервер не прислал значение' : serverSync ? 'включена' : 'выключена';

  $: volumeNumber = parseVolume(volumeText);
  $: volumeProblem = touched ? volumeMessage(volumeText) : null;
  $: sliderValue = volumeNumber ?? serverVolume ?? VOLUME_MAX;

  // Remounting the panel (view switch) must not flash an empty volume field:
  // the cached server snapshot fills the draft before the refresh round-trip.
  $: if (!draftInitialized && snapshot !== null) {
    draftInitialized = true;
    applyServerValues(snapshot);
  }

  $: plan = planSettingsSave(snapshot, { volumeText, syncEnabled, syncTouched });
  $: dirty = plan.changedKeys.length > 0;
  $: changedLabel = plan.changedKeys.map((key) => settingLabel(key)).join(', ');
  $: saveDisabled = !canEditAny || !dirty || $settingsSaving || plan.errors.length > 0;
  $: editableLabel = editable.map((key) => settingLabel(key)).join(', ');
  $: readOnlyRows = buildReadOnlyRows(snapshot);

  onMount(() => {
    void refreshAllSettings().then(() => {
      if ($settingsSnapshot !== null) applyServerValues($settingsSnapshot);
    });
  });

  onDestroy(() => {
    // A dialog unmounted mid-action must not leave the promise hanging.
    const resolve = pinResolve;
    pinResolve = null;
    if (resolve) resolve(null);
  });

  function volumeMessage(text: string): string | null {
    if (text.trim() === '') return 'Укажите громкость от 0 до 100.';
    return volumeError(text);
  }

  function applyServerValues(next: SettingsSnapshot): void {
    const draft = draftFromSnapshot(next);
    volumeText = draft.volumeText;
    syncEnabled = draft.syncEnabled;
    syncTouched = false;
    touched = false;
  }

  function buildReadOnlyRows(next: SettingsSnapshot | null): ReadOnlyRow[] {
    if (next === null) return [];
    const rows: ReadOnlyRow[] = [];
    const valueOf = (key: string): SettingValue | undefined =>
      Object.prototype.hasOwnProperty.call(next.values, key) ? next.values[key] : undefined;

    for (const key of [VOLUME_KEY, SYNC_ENABLED_KEY]) {
      if (next.editable.includes(key)) continue;
      const value = valueOf(key);
      rows.push({
        key,
        label: settingLabel(key),
        value: value === undefined ? 'сервер не прислал значение' : formatSettingValue(value),
        note: settingDescription(key),
      });
    }

    for (const key of next.unknownKeys) {
      const value = valueOf(key);
      rows.push({
        key,
        label: key,
        value: value === undefined ? 'значение не прочитано' : formatSettingValue(value),
        note: 'Ключ незнаком этой версии интерфейса — показан как есть, без контрола.',
      });
    }

    for (const key of next.unreadableKeys) {
      rows.push({
        key,
        label: key,
        value: 'значение не удалось прочитать',
        note: 'Сервер прислал не скаляр (объект/массив/пустую строку).',
      });
    }

    return rows;
  }

  function clock(value: number): string {
    return new Date(value).toLocaleTimeString('ru-RU', {
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

  function updateVolume(value: string): void {
    volumeText = value;
    touched = true;
  }

  function resetDraft(): void {
    if (snapshot === null) return;
    applyServerValues(snapshot);
    notify('Изменения отменены: показаны значения с сервера.');
  }

  async function refresh(): Promise<void> {
    pinError = '';
    await refreshAllSettings();
    if ($settingsSnapshot !== null) applyServerValues($settingsSnapshot);
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
        await withSettingsPinRetry(operation, askPin);
        return { ok: true };
      } catch (error) {
        if (error instanceof SettingsPinCancelledError) {
          return { ok: false, message: 'Действие отменено: PIN не введён.' };
        }
        if (isRetryableSettingsPinError(error) && !onPinRequired) {
          pinError = describeSettingsError(error);
          continue;
        }
        return { ok: false, message: describeSettingsError(error) };
      }
    }
    return { ok: false, message: 'PIN не принят: настройки не изменены.' };
  }

  async function save(event?: SubmitEvent): Promise<void> {
    event?.preventDefault();
    if ($settingsSaving || snapshot === null) return;
    touched = true;
    const current = planSettingsSave(snapshot, { volumeText, syncEnabled, syncTouched });
    if (current.errors.length > 0) {
      notify(current.errors.join(' '), 'error');
      return;
    }
    if (current.changedKeys.length === 0) {
      notify('Изменений нет: сохранять нечего.');
      return;
    }

    actionMessage = '';
    pinError = '';
    const label = current.changedKeys.map((key) => settingLabel(key)).join(', ');
    const outcome = await mutateWithPin(async () => {
      await saveSettings(current.changes);
    });
    if (!outcome.ok) {
      notify(outcome.message, 'error');
      return;
    }
    if ($settingsSnapshot !== null) applyServerValues($settingsSnapshot);
    // A PIN login may have just opened the session cookie: refresh the fact card.
    void refreshAuthInfo();
    notify(`Сохранено: ${label}.`);
  }

  async function runBackup(): Promise<void> {
    if ($serviceBusy !== null) return;
    actionMessage = '';
    pinError = '';
    const outcome = await mutateWithPin(async () => {
      await requestBackup();
    });
    if (!outcome.ok) {
      notify(outcome.message, 'error');
      return;
    }
    void refreshAuthInfo();
    notify('Резервная копия создана: список перечитан с сервера.');
  }

  async function runMaintenance(): Promise<void> {
    if ($serviceBusy !== null) return;
    actionMessage = '';
    pinError = '';
    const outcome = await mutateWithPin(async () => {
      await requestMaintenance();
    });
    if (!outcome.ok) {
      notify(outcome.message, 'error');
      return;
    }
    void refreshAuthInfo();
    notify('Обслуживание выполнено: сервер вернул успех, список копий перечитан.');
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section
  class="settings material-content"
  aria-labelledby="settings-title"
  aria-busy={$settingsLoading || $settingsSaving}
>
  <header class="settings-head">
    <div>
      <span class="eyebrow">Система</span>
      <h1 id="settings-title">Настройки</h1>
      <p class="sub">
        Значения хранит сервер: здесь видно их текущее состояние. Из клиента
        меняется только то, что сервер разрешает, — громкость по умолчанию и,
        если включено, фоновая синхронизация. Остальное — только просмотр.
      </p>
    </div>
    <div class="head-actions">
      {#if snapshot !== null}
        <span class="chip" class:editable={canEditAny} class:readonly={!canEditAny}>
          {canEditAny ? 'Изменяемо: ' + editable.length : 'Только просмотр'}
        </span>
      {/if}
      {#if $settingsUpdatedAt > 0}
        <span class="updated" title="Когда настройки в последний раз сверялись с сервером">
          обновлено {clock($settingsUpdatedAt)}
        </span>
      {/if}
      <button
        type="button"
        class="ghost"
        disabled={$settingsLoading || $settingsSaving}
        onclick={() => void refresh()}
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

  {#if $settingsRouteMissing}
    <div class="notice error" role="alert">
      <strong>Сервер не поддерживает настройки.</strong>
      <span>
        <code>GET /api/v1/settings</code> вернул 404: в этой сборке backend нет
        маршрутов P8. Значения не выдумываются, форма не показывается — включите
        P8 backend и повторите.
      </span>
      <button
        type="button"
        class="ghost small"
        disabled={$settingsLoading}
        onclick={() => void refresh()}
      >
        Повторить
      </button>
    </div>
  {:else if $settingsLoading && snapshot === null}
    <div class="nx-cards" aria-hidden="true">
      {#each [0, 1, 2] as key (key)}
        <div class="nx-card skeleton-card">
          <span class="skeleton-line short"></span>
          <span class="skeleton-line"></span>
          <span class="skeleton-line"></span>
        </div>
      {/each}
    </div>
    <span class="sr-only" role="status">Загружаем настройки…</span>
  {:else if snapshot === null}
    <div class="notice error" role="alert">
      <span>{$settingsError ?? 'Настройки не загружены.'}</span>
      <button
        type="button"
        class="ghost small"
        disabled={$settingsLoading}
        onclick={() => void refresh()}
      >
        Повторить
      </button>
    </div>
  {:else}
    {#if $settingsError}
      <div class="notice error" role="alert">
        <span>{$settingsError}</span>
        <button
          type="button"
          class="ghost small"
          disabled={$settingsLoading}
          onclick={() => void refresh()}
        >
          Повторить
        </button>
      </div>
    {/if}

    <form class="settings-form" onsubmit={(event) => void save(event)} novalidate>
      <div class="nx-cards">
        <fieldset
          class="nx-card"
          disabled={!volumeEditable || $settingsSaving}
          aria-describedby="settings-volume-hint"
        >
          <legend>Плеер</legend>
          <p class="card-sub">{settingDescription(VOLUME_KEY)}</p>

          <label class="field-label" for="settings-volume">Громкость по умолчанию</label>
          <div class="volume-row">
            <input
              id="settings-volume"
              class="slider"
              type="range"
              min={VOLUME_MIN}
              max={VOLUME_MAX}
              step="1"
              value={sliderValue}
              aria-describedby="settings-volume-hint"
              aria-valuetext={`${sliderValue} процентов`}
              oninput={(event) => updateVolume(event.currentTarget.value)}
            />
            <input
              class="number"
              type="number"
              inputmode="numeric"
              min={VOLUME_MIN}
              max={VOLUME_MAX}
              step="1"
              value={volumeText}
              aria-label="Громкость по умолчанию, число от 0 до 100"
              aria-invalid={volumeProblem !== null}
              aria-describedby="settings-volume-hint"
              oninput={(event) => updateVolume(event.currentTarget.value)}
              onblur={() => (touched = true)}
            />
            <span class="unit" aria-hidden="true">%</span>
          </div>

          {#if volumeProblem}
            <p class="field-error" role="alert">{volumeProblem}</p>
          {/if}
          <p id="settings-volume-hint" class="hint">
            Целое число {VOLUME_MIN}–{VOLUME_MAX}. Новое значение применяется сразу
            после сохранения. Сейчас на сервере: {serverVolumeText}.
          </p>
          {#if !volumeEditable}
            <p class="hint read-only-hint">
              Только просмотр: сервер не разрешает менять громкость из интерфейса.
            </p>
          {/if}
        </fieldset>

        <fieldset
          class="nx-card"
          disabled={!syncEditable || $settingsSaving}
          aria-describedby="settings-sync-hint"
        >
          <legend>Синхронизация</legend>
          <p class="card-sub">{settingDescription(SYNC_ENABLED_KEY)}</p>

          <div class="switch-row">
            <input
              id="settings-sync"
              class="switch"
              type="checkbox"
              role="switch"
              bind:checked={syncEnabled}
              aria-describedby="settings-sync-hint"
              onchange={() => (syncTouched = true)}
            />
            <label class="switch-label" for="settings-sync">
              Фоновая синхронизация {syncEnabled ? 'включена' : 'выключена'}
            </label>
          </div>

          <p id="settings-sync-hint" class="hint">
            {#if syncEditable}
              Включение запускает планировщик на сервере, выключение останавливает его.
            {:else}
              Только для чтения: состояние планировщика берётся из
              <code>sync.enabled</code> в config.yaml и применяется при старте
              сервера — изменить его здесь нельзя.
            {/if}
            Сейчас на сервере: {serverSyncText}.
          </p>
        </fieldset>
      </div>

      <div class="form-actions">
        <button type="submit" class="primary" disabled={saveDisabled}>
          {$settingsSaving ? 'Сохраняем…' : 'Сохранить'}
        </button>
        <button
          type="button"
          class="ghost"
          disabled={!dirty || $settingsSaving}
          onclick={resetDraft}
        >
          Сбросить
        </button>
        <p class="hint actions-hint" id="settings-form-hint">
          {#if plan.errors.length > 0}
            Исправьте значение: {plan.errors.join(' ')}
          {:else if dirty}
            Изменения: {changedLabel}.
          {:else if canEditAny}
            Изменений нет.
          {:else}
            Сервер не разрешает менять эти ключи из клиента.
          {/if}
          Отправляются только изменённые ключи.
          {#if editableLabel}
            Сервер разрешает менять: {editableLabel}.
          {/if}
        </p>
      </div>
    </form>

    <div class="nx-cards">
      <section class="nx-card" aria-labelledby="settings-readonly-title">
        <h2 id="settings-readonly-title">Только просмотр</h2>
        {#if readOnlyRows.length === 0}
          <p class="hint">
            Все ключи из ответа сервера доступны для редактирования — отдельных
            значений только для чтения нет.
          </p>
        {:else}
          <dl class="facts">
            {#each readOnlyRows as row (row.key)}
              <div class="fact">
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
                {#if row.note}<span class="fact-note">{row.note}</span>{/if}
              </div>
            {/each}
          </dl>
        {/if}
      </section>

      <section class="nx-card" aria-labelledby="settings-boundaries-title">
        <h2 id="settings-boundaries-title">Что здесь не настраивается</h2>
        <ul class="boundary-list">
          <li>
            <strong>Wi-Fi и точка доступа</strong> — страница «Сеть» и ручной
            AP-onboarding (<code>scripts/ap-onboarding.sh</code>); телефонная
            проверка AP — за владельцем.
          </li>
          <li>
            <strong>Обновление ПО (OTA)</strong> — не реализовано: install.sh /
            update.sh запускаются на Pi, кнопки обновления в интерфейсе нет.
          </li>
          <li>
            <strong>Восстановление из копии</strong> — только CLI
            <code>scripts/restore.sh</code>: live API restore отсутствует, это
            осознанная граница.
          </li>
          {#if $serviceRouteMissing}
            <li>
              <strong>Резервные копии и обслуживание</strong> — нет в этой сборке:
              <code>GET /api/v1/system/backups</code> вернул 404.
            </li>
          {/if}
        </ul>
        <p class="hint">
          Пункты без API показаны текстом, а не кнопками: фиктивных действий в
          интерфейсе нет.
        </p>
      </section>
    </div>

    {#if !$serviceRouteMissing}
      <section class="nx-card service" aria-labelledby="settings-service-title">
        <div class="card-head">
          <h2 id="settings-service-title">Сервис</h2>
          <span class="status ok">API доступен</span>
        </div>
        <p class="card-sub">
          Резервные копии базы и обслуживание сервера. Создание копии и запуск
          обслуживания доступны из интерфейса; восстановление — на самом сервере.
        </p>

        {#if $serviceLoading && $backups.length === 0 && !$serviceError}
          <p class="hint">Читаем список резервных копий…</p>
        {:else if $serviceError}
          <div class="notice error" role="alert">
            <span>{$serviceError}</span>
            <button
              type="button"
              class="ghost small"
              disabled={$serviceLoading || $serviceBusy !== null}
              onclick={() => void refresh()}
            >
              Повторить
            </button>
          </div>
        {:else}
          {#if $backups.length === 0}
            <p class="idle-line">Резервных копий пока нет.</p>
            <p class="hint">
              Копия создаётся кнопкой ниже или автоматически перед real-импортом и
              real-загрузкой.
            </p>
          {:else}
            <ul class="backups">
              {#each $backups as item (item.name)}
                <li>
                  <span class="backup-name" title={item.name}>{item.name}</span>
                  <span class="backup-meta">
                    {formatDateTime(item.created_at)} · {formatBackupSize(item.size_bytes)}
                  </span>
                  <span
                    class="status"
                    class:ok={item.verified === true}
                    class:bad={item.verified === false}
                  >
                    {item.verified === true
                      ? 'проверена'
                      : item.verified === false
                        ? 'не проверена'
                        : 'статус не прислан'}
                  </span>
                </li>
              {/each}
            </ul>
            {#if $backupsTotal > $backups.length}
              <p class="hint">показано {$backups.length} из {$backupsTotal}</p>
            {/if}
          {/if}

          <div class="service-actions">
            <button
              type="button"
              class="ghost"
              disabled={$serviceBusy !== null}
              onclick={() => void runBackup()}
            >
              {$serviceBusy === 'backup' ? 'Создаём…' : 'Создать резервную копию'}
            </button>
            <button
              type="button"
              class="ghost"
              disabled={$serviceBusy !== null}
              onclick={() => void runMaintenance()}
            >
              {$serviceBusy === 'maintenance' ? 'Выполняем…' : 'Обслуживание'}
            </button>
          </div>

          <p class="hint">
            Восстановление — только CLI <code>scripts/restore.sh</code>: сервис
            останавливается, копия проверяется, БД заменяется. Обслуживание удаляет
            старые <code>.part</code>/временные файлы, делает WAL checkpoint и
            <code>PRAGMA quick_check</code>; по таймеру оно запускается раз в сутки
            при заданном <code>maintenance.interval_s</code> (по умолчанию выключен).
          </p>
        {/if}
      </section>
    {/if}
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="settings-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="settings-pin-title">PIN для изменения настроек</h2>
        <p>
          Сохранение настроек и сервисные операции — изменяющие действия. Введите
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
  .settings {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .settings-head {
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
    line-height: 1.5;
  }

  .hint code,
  .boundary-list code {
    font-family: inherit;
    font-size: 0.94em;
    color: #c4b5fd;
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
  }

  .chip.editable {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.3);
    background: rgba(34, 211, 238, 0.09);
  }

  .chip.readonly {
    color: #cbd5e1;
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

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button:focus-visible,
  input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
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
    flex: 1 1 260px;
  }

  .notice code {
    font-family: inherit;
    font-size: 0.74rem;
    color: #c4b5fd;
  }

  .nx-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
    align-items: start;
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 12px;
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

  fieldset.nx-card {
    border: 1px solid var(--border);
    min-inline-size: 0;
  }

  fieldset.nx-card[disabled] {
    opacity: 0.92;
  }

  .nx-card legend,
  .nx-card h2 {
    margin: 0;
    padding: 0;
    font-size: 0.96rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }

  .nx-card legend + .card-sub {
    margin-top: 2px;
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

  .field-label {
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
  }

  .volume-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .slider {
    flex: 1 1 150px;
    min-width: 120px;
    accent-color: var(--accent);
    height: 22px;
  }

  .number {
    width: 84px;
    padding: 8px 10px;
    border-radius: 11px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.7);
    color: var(--text);
    font-size: 0.86rem;
    font-variant-numeric: tabular-nums;
  }

  .unit {
    color: var(--muted);
    font-size: 0.78rem;
  }

  .switch-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .switch {
    appearance: none;
    -webkit-appearance: none;
    flex: none;
    width: 46px;
    height: 26px;
    margin: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.12);
    position: relative;
    cursor: pointer;
    transition: background 160ms ease, border-color 160ms ease;
  }

  .switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: #e2e8f7;
    transition: transform 160ms ease;
  }

  .switch:checked {
    background: var(--accent);
    border-color: transparent;
  }

  .switch:checked::after {
    transform: translateX(20px);
  }

  .switch:disabled {
    cursor: not-allowed;
  }

  .switch-label {
    font-size: 0.82rem;
    color: var(--text);
    cursor: pointer;
    overflow-wrap: anywhere;
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

  .read-only-hint {
    color: #fcd34d;
  }

  .form-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 12px 14px;
    border-radius: 16px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.42);
  }

  .form-actions .primary {
    padding: 9px 18px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    color: #0b0715;
    border: 0;
    background: var(--accent);
    box-shadow: 0 12px 26px -16px rgba(139, 92, 246, 0.85);
  }

  .form-actions .primary:hover:not(:disabled) {
    filter: brightness(1.06);
  }

  .actions-hint {
    flex: 1 1 260px;
    min-width: 0;
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
    color: var(--muted);
    font-size: 0.7rem;
    line-height: 1.45;
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

  .service {
    width: 100%;
  }

  .idle-line {
    margin: 0;
    color: var(--text);
    font-size: 0.84rem;
    font-weight: 600;
  }

  .backups {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .backups li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 4px 12px;
    padding: 9px 11px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: rgba(6, 9, 16, 0.42);
  }

  .backup-name {
    min-width: 0;
    font-size: 0.76rem;
    color: var(--text);
    overflow-wrap: anywhere;
  }

  .backup-meta {
    grid-column: 1;
    color: var(--muted);
    font-size: 0.68rem;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .backups .status {
    grid-column: 2;
    grid-row: 1 / span 2;
    justify-self: end;
  }

  .service-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
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

  .status.ok {
    color: #86efac;
    border-color: rgba(134, 239, 172, 0.28);
    background: rgba(16, 185, 129, 0.09);
  }

  .status.bad {
    color: #fca5a5;
    border-color: rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.1);
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
    border: 0;
    background: var(--accent);
  }

  @media (max-width: 900px) {
    .settings-head {
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
    .form-actions .primary,
    .form-actions .ghost {
      flex: 1 1 auto;
      min-height: 40px;
    }
    .backups li {
      grid-template-columns: minmax(0, 1fr);
    }
    .backups .status {
      grid-column: 1;
      grid-row: auto;
      justify-self: start;
    }
  }

  @media (max-width: 480px) {
    .settings {
      padding: 16px;
      border-radius: 20px;
    }
    .fact {
      padding: 7px 8px;
    }
    .form-actions .primary,
    .form-actions .ghost {
      width: 100%;
      min-height: 42px;
    }
    .actions-hint {
      flex-basis: 100%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .switch,
    .switch::after,
    .nx-card,
    .skeleton-line {
      transition: none;
      animation: none;
    }
  }
</style>