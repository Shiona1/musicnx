<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    LYRICS_OFFSET_LIMIT_MS,
    LYRICS_OFFSET_STEP_MS,
    activeLineIndex,
    clampOffsetMs,
    describeLyricsError,
    formatOffsetMs,
    isRetryableLyricsPinError,
    lyricsSourceLabel,
    LyricsPinCancelledError,
  } from '../lib/lyrics';
  import { loadLyrics, lyricsEntry, saveLyricsOffset } from '../lib/lyricsStores';
  import type { LyricsDTO, LyricsLine } from '../lib/lyrics';
  import type { LyricsEntry, LyricsStatus } from '../lib/lyricsStores';

  /**
   * P5 karaoke panel — reads `GET /lyrics/{track_id}` and highlights the active
   * LRC line from the `position` prop. Clicking a line seeks through the
   * injected `onSeek`; the offset controls (±0.5 s) save with a PIN-protected
   * `PUT /lyrics/{track_id}/offset` (the panel asks for the PIN, logs in via
   * `POST /auth/pin` and replays the exact request).
   *
   * The component owns no persistence: the server keeps the offset, and the
   * shared store skips caching into localStorage (there is none).
   */
  export let trackId: number | null = null;
  /** Player position in milliseconds (interpolated by App). */
  export let position = 0;
  export let onSeek: (positionMs: number) => void = () => {};
  /** Routes feedback to the app toast; without it a local line is rendered. */
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;

  const MAX_PIN_ROUNDS = 3;
  /** After a manual scroll the highlight does not yank the view for a while. */
  const MANUAL_SCROLL_HOLD_MS = 6000;

  let scroller: HTMLOListElement | null = null;
  let pendingOffset: number | null = null;
  let saving = false;
  let localMessage = '';
  let localKind: 'ok' | 'error' = 'ok';
  let pinError = '';
  let showPin = false;
  let pin = '';
  let pinResolve: ((value: string | null) => void) | null = null;
  let reduceMotion = false;
  let manualScrollAt = 0;
  let autoScrollIndex = -2;
  let programmaticUntil = 0;

  let entry: LyricsEntry = $lyricsEntry;
  let status: LyricsStatus = 'idle';
  let data: LyricsDTO | null = null;
  let lines: LyricsLine[] = [];
  let hasLines = false;
  let plain: string | null = null;
  let hasPlain = false;
  let savedOffset = 0;
  let effectiveOffset = 0;
  let offsetDirty = false;
  let activeIndex = -1;

  onMount(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    reduceMotion = query.matches;
    const onChange = (event: MediaQueryListEvent): void => {
      reduceMotion = event.matches;
    };
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  });

  onDestroy(() => {
    // A panel unmounted while the PIN dialog is open must not leave a promise
    // hanging: resolve it as "cancelled", so the save stops cleanly.
    const resolve = pinResolve;
    pinResolve = null;
    if (resolve) resolve(null);
  });

  function pickData(activeTrackId: number | null, current: LyricsEntry): LyricsDTO | null {
    if (activeTrackId === null || current.trackId !== activeTrackId) return null;
    return current.data;
  }

  function statusFor(activeTrackId: number | null, current: LyricsEntry): LyricsStatus {
    if (activeTrackId === null) return 'idle';
    return current.trackId === activeTrackId ? current.status : 'loading';
  }

  /** Runs whenever `trackId` changes: reset per-track UI and (re)load. */
  function syncTrack(nextTrackId: number | null): void {
    // A PIN dialog that belonged to the previous track must not save against it.
    if (pinResolve !== null) cancelPin();
    pendingOffset = null;
    pinError = '';
    localMessage = '';
    autoScrollIndex = -2;
    void loadLyrics(nextTrackId);
  }

  let renderedLines: LyricsLine[] | null = null;

  /**
   * Called on every lyric/position change. A new lyric resets the view, then the
   * active line is centred; both happen in one reactive statement, so the reset
   * can never undo an autoscroll from the same tick.
   */
  function syncLyricView(nextLines: LyricsLine[], index: number): void {
    if (nextLines !== renderedLines) {
      renderedLines = nextLines;
      autoScrollIndex = -2;
      if (scroller !== null) {
        programmaticUntil = performance.now() + 200;
        scroller.scrollTop = 0;
      }
    }
    if (index === autoScrollIndex) return;
    autoScrollIndex = index;
    void scrollActiveIntoView();
  }

  async function scrollActiveIntoView(): Promise<void> {
    if (!hasLines || activeIndex < 0 || scroller === null) return;
    if (Date.now() - manualScrollAt < MANUAL_SCROLL_HOLD_MS) return;
    await tick();
    const container = scroller;
    if (container === null) return;
    const node = container.querySelector<HTMLElement>(`[data-line-index="${activeIndex}"]`);
    if (node === null) return;
    const target = node.offsetTop - container.clientHeight / 2 + node.offsetHeight / 2;
    const maxTop = Math.max(0, container.scrollHeight - container.clientHeight);
    const top = Math.min(Math.max(0, target), maxTop);
    if (Math.abs(container.scrollTop - top) < 4) return;
    // Smooth scrolling is skipped for users who asked for less motion.
    programmaticUntil = performance.now() + (reduceMotion ? 120 : 900);
    container.scrollTo({ top, behavior: reduceMotion ? 'auto' : 'smooth' });
  }

  /** Any scroll that is not ours means the user took over: hold autoscroll. */
  function handleScroll(): void {
    if (performance.now() < programmaticUntil) return;
    manualScrollAt = Date.now();
  }

  function seekToLine(index: number): void {
    const line = lines[index];
    if (line === undefined) return;
    // The offset shifts lyrics against audio, so the same shift applies to seek.
    onSeek(Math.max(0, Math.round(line.time_ms + effectiveOffset)));
  }

  function handleLinesKeydown(index: number, event: KeyboardEvent): void {
    if (!Number.isFinite(index)) return;
    let next = -1;
    if (event.key === 'ArrowDown') next = index + 1;
    else if (event.key === 'ArrowUp') next = index - 1;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = lines.length - 1;
    else return;
    if (next < 0 || next >= lines.length) return;
    event.preventDefault();
    const node = scroller?.querySelector<HTMLButtonElement>(`button[data-line-index="${next}"]`);
    node?.focus();
  }

  function stepOffset(delta: number): void {
    if (!hasLines) return;
    pendingOffset = clampOffsetMs(effectiveOffset + delta);
  }

  function resetOffset(): void {
    if (!hasLines) return;
    pendingOffset = clampOffsetMs(0);
  }

  function discardPendingOffset(): void {
    pendingOffset = null;
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

  /** Asks for the PIN through the built-in dialog (no shared modal exists). */
  function askPin(): Promise<string | null> {
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

  function handleWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && showPin) cancelPin();
  }

  async function persistOffset(): Promise<void> {
    const value = pendingOffset;
    const requestTrackId = trackId;
    // `saving` is read directly: the reactive `offsetDirty` may still lag a tick.
    if (value === null || requestTrackId === null || saving) return;
    saving = true;
    pinError = '';
    localMessage = '';
    try {
      for (let round = 0; round < MAX_PIN_ROUNDS; round += 1) {
        try {
          const updated = await saveLyricsOffset(requestTrackId, value, askPin);
          if (trackId === requestTrackId) pendingOffset = null;
          pinError = '';
          notify(`Смещение сохранено: ${formatOffsetMs(updated.offset_ms)}`);
          return;
        } catch (error) {
          if (error instanceof LyricsPinCancelledError) {
            notify('Смещение не сохранено: PIN не введён.', 'error');
            return;
          }
          if (isRetryableLyricsPinError(error)) {
            // Keep the built-in dialog going with the exact reason.
            pinError = describeLyricsError(error);
            continue;
          }
          notify(describeLyricsError(error), 'error');
          return;
        }
      }
      notify('PIN не принят: смещение не сохранено.', 'error');
    } finally {
      saving = false;
    }
  }

  function focusInput(node: HTMLInputElement): { destroy: () => void } {
    const timer = window.setTimeout(() => node.focus(), 0);
    return { destroy: () => window.clearTimeout(timer) };
  }

  $: syncTrack(trackId);
  $: entry = $lyricsEntry;
  $: status = statusFor(trackId, entry);
  $: data = pickData(trackId, entry);
  $: lines = data?.lines ?? [];
  $: hasLines = lines.length > 0;
  $: plain = data?.plain ?? null;
  $: hasPlain = !hasLines && plain !== null && plain.trim() !== '';
  $: savedOffset = data?.offset_ms ?? 0;
  $: effectiveOffset = pendingOffset ?? savedOffset;
  $: offsetDirty = pendingOffset !== null && pendingOffset !== savedOffset;
  $: activeIndex = hasLines ? activeLineIndex(lines, position, effectiveOffset) : -1;
  $: sourceLabel = lyricsSourceLabel(data);
  $: languageLabel = data?.language ? data.language.toUpperCase() : null;
  $: syncLyricView(lines, activeIndex);
</script>

<svelte:window onkeydown={handleWindowKeydown} />

<section class="lyrics material-solid" aria-labelledby="lyrics-title">
  <header class="head">
    <h2 id="lyrics-title">
      <span class="head-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" focusable="false">
          <path d="M4 5h11v2H4zm0 5h11v2H4zm0 5h11v2H4zm14.4-7.5v9.1a2.4 2.4 0 1 0 1.6 2.25V7.5z" />
        </svg>
      </span>
      Текст песни
    </h2>
    <div class="badges">
      {#if status === 'available' && sourceLabel}
        <span class="chip">{sourceLabel}</span>
      {/if}
      {#if status === 'available' && languageLabel}
        <span class="chip lang">{languageLabel}</span>
      {/if}
      {#if status === 'available' && !hasLines && hasPlain}
        <span class="chip plain">без синхронизации</span>
      {/if}
    </div>
  </header>

  {#if status === 'idle'}
    <p class="state">Выберите трек — текст загрузится автоматически.</p>
  {:else if status === 'loading'}
    <div class="skeleton" aria-hidden="true">
      <span style="width: 72%"></span>
      <span style="width: 88%"></span>
      <span style="width: 54%"></span>
      <span style="width: 80%"></span>
    </div>
    <p class="state" aria-live="polite">Загружаем текст…</p>
  {:else if status === 'error'}
    <div class="state-block">
      <p class="state" role="status" aria-live="polite">
        {#if entry.routeMissing}
          Backend ещё не отдаёт маршруты текстов (P5): текст недоступен на этом сервере.
        {:else}
          {entry.error ?? 'Не удалось загрузить текст песни.'}
        {/if}
      </p>
      {#if trackId !== null}
        <button class="retry" type="button" onclick={() => void loadLyrics(trackId)}>
          Повторить загрузку
        </button>
      {/if}
    </div>
  {:else if status === 'none'}
    <div class="state-block">
      <p class="state" role="status" aria-live="polite">
        Текст этой песни не найден. Сервер не нашёл ни LRC, ни обычного текста.
      </p>
      {#if trackId !== null}
        <button class="retry" type="button" onclick={() => void loadLyrics(trackId)}>
          Повторить загрузку
        </button>
      {/if}
    </div>
  {:else if status === 'available'}
    {#if hasLines}
      <div
        class="offset-row"
        role="group"
        aria-label="Смещение текста относительно музыки"
      >
        <span class="offset-label">Смещение</span>
        <button
          class="step"
          type="button"
          aria-label="Сдвинуть текст на полсекунды раньше"
          title="Раньше на 0,5 с"
          disabled={saving || effectiveOffset <= -LYRICS_OFFSET_LIMIT_MS}
          onclick={() => stepOffset(-LYRICS_OFFSET_STEP_MS)}
        >
          −0,5 с
        </button>
        <span class="offset-value" class:dirty={offsetDirty}>{formatOffsetMs(effectiveOffset)}</span>
        <button
          class="step"
          type="button"
          aria-label="Сдвинуть текст на полсекунды позже"
          title="Позже на 0,5 с"
          disabled={saving || effectiveOffset >= LYRICS_OFFSET_LIMIT_MS}
          onclick={() => stepOffset(LYRICS_OFFSET_STEP_MS)}
        >
          +0,5 с
        </button>
        <button
          class="save"
          type="button"
          aria-label="Сохранить смещение текста на сервере"
          aria-busy={saving}
          disabled={!offsetDirty || saving}
          onclick={() => void persistOffset()}
        >
          {saving ? 'Сохраняем…' : 'Сохранить'}
        </button>
        {#if pendingOffset !== null}
          <button
            class="ghost"
            type="button"
            aria-label="Сбросить смещение текста к нулю"
            disabled={saving || (savedOffset === 0 && !offsetDirty)}
            onclick={resetOffset}
          >
            Сброс
          </button>
          <button
            class="ghost"
            type="button"
            aria-label="Отменить несохранённое смещение"
            disabled={saving}
            onclick={discardPendingOffset}
          >
            Отменить
          </button>
        {/if}
      </div>
      {#if offsetDirty}
        <p class="dirty-note">Смещение ещё не сохранено на сервере.</p>
      {/if}

      <!-- The line list is a scrollable region with roving focus: one tab stop,
           arrow keys/Home/End move between lines, Enter seeks. `aria-live` is
           off because the highlight changes several times per second. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <ol
        class="lines"
        bind:this={scroller}
        tabindex="0"
        aria-label="Строки текста песни"
        aria-live="off"
        onscroll={handleScroll}
      >
        {#each lines as line, index (index)}
          <li>
            <button
              class="line"
              class:active={index === activeIndex}
              class:passed={index < activeIndex}
              type="button"
              data-line-index={index}
              tabindex={index === (activeIndex >= 0 ? activeIndex : 0) ? 0 : -1}
              aria-current={index === activeIndex ? 'true' : undefined}
              aria-label={`Перейти к строке ${index + 1}: ${line.text || 'без слов'}`}
              onclick={() => seekToLine(index)}
              onkeydown={(event) => handleLinesKeydown(index, event)}
            >
              <span class="line-text">{line.text || '♪'}</span>
            </button>
          </li>
        {/each}
      </ol>
    {:else if hasPlain}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div
        class="plain"
        tabindex="0"
        role="region"
        aria-label="Текст песни без синхронизации"
        aria-live="off"
      >
        <p>{plain}</p>
      </div>
    {:else}
      <p class="state">Сервер вернул пустой текст песни.</p>
    {/if}
  {/if}

  {#if localMessage}
    <p class="message" class:error={localKind === 'error'} role="status" aria-live="polite">
      {localMessage}
    </p>
  {/if}
</section>

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    onclick={(event) => event.target === event.currentTarget && cancelPin()}
  >
    <div class="modal material-overlay" role="dialog" aria-modal="true" aria-labelledby="lyrics-pin-title">
      <form onsubmit={submitPin}>
        <h2 id="lyrics-pin-title">PIN для смещения текста</h2>
        <p>
          Сохранение смещения — изменяющее действие, оно защищено PIN-кодом сервера.
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
  .lyrics {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
    padding: clamp(16px, 2vw, 22px);
    border-radius: 26px;
    border: 1px solid var(--border);
    background:
      linear-gradient(160deg, rgba(255, 255, 255, 0.055), rgba(255, 255, 255, 0.012)),
      rgba(10, 14, 24, 0.58);
    box-shadow: 0 24px 60px -34px rgba(0, 0, 0, 0.9);
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }

  h2 {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: 1rem;
    letter-spacing: -0.01em;
  }

  .head-icon {
    color: var(--accent-2);
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .chip {
    padding: 4px 9px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  .chip.lang {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.28);
    background: rgba(34, 211, 238, 0.08);
  }

  .chip.plain {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.25);
    background: rgba(252, 211, 77, 0.08);
  }

  .state {
    margin: 0;
    color: var(--muted);
    font-size: 0.82rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .state-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }

  .retry {
    padding: 7px 14px;
    min-height: 34px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 700;
    transition:
      background 140ms ease,
      border-color 140ms ease;
  }

  .retry:hover {
    background: var(--surface-hover);
    border-color: rgba(34, 211, 238, 0.35);
  }

  .skeleton {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 10px 6px;
  }

  .skeleton span {
    display: block;
    height: 12px;
    border-radius: 999px;
    background: linear-gradient(
      90deg,
      rgba(255, 255, 255, 0.05),
      rgba(255, 255, 255, 0.13),
      rgba(255, 255, 255, 0.05)
    );
    background-size: 200% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  @keyframes shimmer {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  .offset-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 7px;
    padding: 8px 10px;
    border-radius: 14px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.03);
  }

  .offset-label {
    margin-right: 2px;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .offset-value {
    min-width: 5.6ch;
    text-align: center;
    font-size: 0.78rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .offset-value.dirty {
    color: #fcd34d;
  }

  .step,
  .save,
  .ghost {
    padding: 6px 12px;
    min-height: 32px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.74rem;
    font-weight: 700;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      color 140ms ease;
  }

  .step:hover:not(:disabled),
  .ghost:hover:not(:disabled) {
    background: var(--surface-hover);
    border-color: rgba(34, 211, 238, 0.32);
  }

  .save {
    color: #0b0715;
    border: 0;
    background: var(--accent);
  }

  .save:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  .ghost {
    color: var(--muted);
  }

  .dirty-note {
    margin: 0;
    color: #fcd34d;
    font-size: 0.7rem;
  }

  .lines {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 2px;
    list-style: none;
    margin: 0;
    padding: 6px 8px;
    max-height: clamp(220px, 42vh, 460px);
    overflow-y: auto;
    overscroll-behavior: contain;
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.045);
    background: rgba(4, 7, 14, 0.35);
  }

  .lines:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  .lines li {
    margin: 0;
  }

  .line {
    display: block;
    width: 100%;
    padding: 8px 12px;
    border-radius: 10px;
    border: 1px solid transparent;
    color: var(--muted);
    text-align: left;
    font-size: clamp(0.92rem, 1.1vw, 1.02rem);
    line-height: 1.45;
    transition:
      color 160ms ease,
      background 160ms ease;
  }

  .line:hover {
    color: var(--text);
    background: rgba(255, 255, 255, 0.045);
  }

  .line.passed {
    color: rgba(139, 147, 167, 0.58);
  }

  .line.active {
    color: #f8fafc;
    font-weight: 600;
    background: linear-gradient(90deg, rgba(139, 92, 246, 0.24), rgba(34, 211, 238, 0.12));
    border-color: rgba(34, 211, 238, 0.2);
    box-shadow: inset 2px 0 0 var(--accent-2);
  }

  .line:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: -2px;
  }

  .line-text {
    overflow-wrap: anywhere;
  }

  .plain {
    max-height: clamp(220px, 42vh, 460px);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 12px 14px;
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.045);
    background: rgba(4, 7, 14, 0.35);
    color: #d5daeb;
    font-size: 0.9rem;
    line-height: 1.65;
  }

  .plain:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  .plain p {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .message {
    margin: 0;
    color: var(--muted);
    font-size: 0.72rem;
    line-height: 1.4;
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
    display: block;
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
    color: var(--text);
  }

  .modal-actions .primary {
    padding: 8px 16px;
    min-height: 32px;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 700;
    color: #0b0715;
    background: var(--accent);
  }

  .modal-actions .primary:disabled {
    opacity: 0.45;
  }

  @media (max-width: 860px) {
    .lines,
    .plain {
      max-height: clamp(180px, 36vh, 340px);
    }
  }

  @media (max-width: 560px) {
    .lyrics {
      padding: 15px;
      border-radius: 22px;
    }
    .step,
    .save,
    .ghost {
      padding: 8px 13px;
      min-height: 38px;
    }
    .line {
      padding: 9px 10px;
    }
    .modal {
      padding: 16px;
    }
  }
</style>
