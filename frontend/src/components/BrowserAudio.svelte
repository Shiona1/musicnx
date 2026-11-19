<script lang="ts">
  /**
   * Browser zone (docs/API.md §6.5): mpv stays the master clock, the browser
   * mirrors it with a same-origin `<audio>` element. The track id is the only
   * thing sent to the server — no token, no signed URL, no CORS.
   */
  import { onDestroy } from 'svelte';
  import { streamUrl } from '../lib/yandex';
  import type { PlayerState, PlayerStatus } from '../lib/types';

  export let playerState: PlayerState | null = null;
  /** Interpolated server position in milliseconds (2 Hz WS events + rAF). */
  export let position = 0;
  /** Mirrors `audio.browser_zone` from GET /system/status; false → no <audio>. */
  export let browserZone = false;
  /** 0..100; defaults to the player state volume. */
  export let volume: number | null = null;
  /** Explicit track override; defaults to playerState.track.id. */
  export let trackId: number | null = null;
  export let onError: ((message: string) => void) | null = null;

  /** Drift above this is a real desync and gets corrected (docs/API.md §6.5). */
  const DRIFT_TOLERANCE_MS = 500;

  let audioEl: HTMLAudioElement | null = null;
  let blocked = false;
  let failed = false;
  let destroyed = false;
  let playPending = false;

  $: currentTrackId = trackId ?? playerState?.track?.id ?? null;
  $: src = browserZone && currentTrackId !== null ? streamUrl(currentTrackId) : null;
  $: status = (playerState?.status ?? 'stopped') as PlayerStatus;
  $: effectiveVolume = clampPercent(volume ?? playerState?.volume ?? 70);

  // Every dependency is passed explicitly: reactive statements track only the
  // variables they read, so a bare sync() call would never re-run on position.
  $: if (audioEl) {
    syncAudio(audioEl, status, position, effectiveVolume, src);
  }

  function clampPercent(value: number): number {
    if (!Number.isFinite(value)) return 0.7;
    return Math.min(1, Math.max(0, value / 100));
  }

  function syncAudio(
    el: HTMLAudioElement,
    state: PlayerStatus,
    positionMs: number,
    level: number,
    source: string | null,
  ): void {
    if (!source) {
      if (!el.paused) el.pause();
      return;
    }
    // App interpolates the position every animation frame, so this runs at
    // ~60 Hz: only touch the DOM when a value actually differs.
    if (el.volume !== level) el.volume = level;

    if (state === 'playing') {
      const drift = Math.abs(el.currentTime * 1000 - positionMs);
      if (drift > DRIFT_TOLERANCE_MS) el.currentTime = Math.max(0, positionMs) / 1000;
      if (el.paused) void playSafely(el);
      return;
    }

    if (!el.paused) el.pause();
    if (state === 'paused') {
      const drift = Math.abs(el.currentTime * 1000 - positionMs);
      if (drift > DRIFT_TOLERANCE_MS) el.currentTime = Math.max(0, positionMs) / 1000;
    } else if (state === 'stopped' || state === 'error') {
      if (el.currentTime > 0) el.currentTime = 0;
    }
    // `buffering` only pauses: the server keeps the authoritative position.
  }

  async function playSafely(el: HTMLAudioElement): Promise<void> {
    if (playPending) return;
    playPending = true;
    try {
      await el.play();
      blocked = false;
    } catch (error) {
      if (error instanceof DOMException && error.name === 'NotAllowedError') {
        // Autoplay policy: without a user gesture the browser refuses to start.
        blocked = true;
      }
      // AbortError is expected when a pause lands while play() is pending.
    } finally {
      playPending = false;
    }
  }

  function retry(): void {
    if (!audioEl) return;
    failed = false;
    blocked = false;
    audioEl.load();
    if (status === 'playing') void playSafely(audioEl);
  }

  function resumeFromGesture(): void {
    if (!audioEl) return;
    void playSafely(audioEl).then(() => {
      if (audioEl) syncAudio(audioEl, status, position, effectiveVolume, src);
    });
  }

  function handleCanPlay(): void {
    if (destroyed) return;
    failed = false;
    if (audioEl && status === 'playing') void playSafely(audioEl);
  }

  function handleError(): void {
    if (destroyed) return;
    failed = true;
    onError?.(
      'Браузер не смог воспроизвести поток. Проверьте соединение с сервером MusicNX.',
    );
  }

  onDestroy(() => {
    destroyed = true;
    const el = audioEl;
    if (!el) return;
    el.pause();
    el.removeAttribute('src');
    el.load();
  });
</script>

{#if browserZone && currentTrackId !== null}
  <audio
    class="browser-audio"
    bind:this={audioEl}
    src={src ?? undefined}
    preload="auto"
    aria-hidden="true"
    oncanplay={handleCanPlay}
    onerror={handleError}
  ></audio>
{/if}

{#if browserZone && (blocked || failed)}
  <div class="audio-hint" role="status">
    <span>
      {failed
        ? 'Поток не открылся в браузере. Сервер мог не получить его у CDN.'
        : 'Браузер ждёт разрешения на звук.'}
    </span>
    {#if blocked}
      <button type="button" class="audio-button" onclick={resumeFromGesture}>
        Включить звук
      </button>
    {:else}
      <button type="button" class="audio-button" onclick={retry}>
        Повторить
      </button>
    {/if}
  </div>
{/if}

<style>
  .browser-audio {
    display: none;
  }

  .audio-hint {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 12px;
    border-radius: 12px;
    border: 1px solid rgba(252, 211, 77, 0.28);
    background: rgba(252, 211, 77, 0.08);
    color: #fde68a;
    font-size: 0.75rem;
  }

  .audio-button {
    padding: 5px 12px;
    border-radius: 999px;
    border: 1px solid rgba(252, 211, 77, 0.4);
    color: #fde68a;
    font-size: 0.72rem;
    font-weight: 700;
  }

  .audio-button:hover {
    background: rgba(252, 211, 77, 0.14);
  }
</style>
