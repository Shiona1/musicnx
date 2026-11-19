<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import Icon from './Icon.svelte';
  import YandexTrackRow from './YandexTrackRow.svelte';
  import { playedSeconds } from '../lib/yandex';
  import type { WaveAction, WaveState } from '../lib/yandex';
  import {
    sendYandexWaveFeedback,
    startYandexWave,
    yandexWave,
    yandexWaveError,
    yandexWaveLoading,
  } from '../lib/yandexStores';

  export let onPlay: (trackId: number) => void;
  export let onEnqueue: ((trackId: number) => void) | null = null;
  export let onWaveChange: ((wave: WaveState) => void) | null = null;
  export let currentTrackId: number | null = null;
  /** Interpolated position in ms, used for the played_seconds skip feedback. */
  export let position = 0;
  export let busy = false;
  /** Opt-in: start the station automatically when the card first mounts. */
  export let autostart = false;

  let starting = false;
  let pending: { trackId: number; action: WaveAction } | null = null;

  $: wave = $yandexWave;
  $: updatedLabel = wave ? formatUpdatedAt(wave.updated_at) : '';

  onMount(() => {
    if (autostart && get(yandexWave) === null) void start();
  });

  function formatUpdatedAt(value: string): string {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  async function start(): Promise<void> {
    if (starting) return;
    starting = true;
    const next = await startYandexWave();
    starting = false;
    if (next) onWaveChange?.(next);
  }

  async function feedback(trackId: number, action: WaveAction): Promise<void> {
    if (pending) return;
    pending = { trackId, action };
    const seconds = trackId === currentTrackId ? playedSeconds(position) : 0;
    const next = await sendYandexWaveFeedback(action, trackId, seconds);
    pending = null;
    if (next) onWaveChange?.(next);
  }
</script>

<section class="wave material-content" aria-labelledby="wave-title">
  <div class="wave-head">
    <div>
      <span class="eyebrow">Яндекс Музыка</span>
      <h2 id="wave-title">Моя волна</h2>
      {#if wave}
        <p class="wave-sub">
          Станция <code>{wave.station}</code>{#if updatedLabel} · обновлено в {updatedLabel}{/if}
        </p>
      {:else}
        <p class="wave-sub">Персональный поток; сервер сам пополняет очередь станции.</p>
      {/if}
    </div>
    <button
      class="primary"
      type="button"
      disabled={starting || $yandexWaveLoading}
      onclick={start}
    >
      {#if starting || $yandexWaveLoading}
        Загрузка…
      {:else if wave}
        Обновить волну
      {:else}
        Включить волну
      {/if}
    </button>
  </div>

  {#if $yandexWaveError}
    <div class="notice error" role="alert">{$yandexWaveError}</div>
  {/if}

  {#if $yandexWaveLoading && !wave}
    <div class="loading" role="status">
      <span class="spinner" aria-hidden="true"></span>
      Запрашиваем треки «Моей волны»…
    </div>
  {:else if !wave || wave.tracks.length === 0}
    <div class="empty">
      <Icon name="music" size={28} />
      <div>
        <strong>Волна не запущена</strong>
        <p>
          Нажмите «Включить волну»: сервер запросит станцию, нормализует треки в
          каталог и покажет их здесь.
        </p>
      </div>
    </div>
  {:else}
    <ul class="wave-list">
      {#each wave.tracks as track, index (track.id)}
        <YandexTrackRow
          {track}
          {index}
          current={track.id === currentTrackId}
          busy={busy || starting}
          feedbackBusy={pending !== null}
          showAlbum={false}
          {onPlay}
          {onEnqueue}
          onFeedback={(action) => feedback(track.id, action)}
        />
      {/each}
    </ul>
    <p class="wave-note">
      Лайк и дизлайк уходят в аккаунт, пропуск — в станцию. Пополнение волны —
      на сервере.
    </p>
  {/if}
</section>

<style>
  .wave {
    border: 1px solid var(--border);
    background:
      linear-gradient(160deg, rgba(139, 92, 246, 0.09), rgba(34, 211, 238, 0.04)),
      rgba(10, 14, 24, 0.62);
    border-radius: 22px;
    padding: clamp(16px, 2vw, 22px);
  }

  .wave-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }

  .eyebrow {
    color: var(--accent-2);
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  h2 {
    margin: 2px 0 3px;
    font-size: 1.06rem;
    letter-spacing: 0.01em;
  }

  .wave-sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.74rem;
    overflow-wrap: anywhere;
  }

  code {
    font-family: inherit;
    font-size: 0.72rem;
    color: #c4b5fd;
  }

  button.primary {
    padding: 9px 16px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    color: #0b0715;
    background: var(--accent);
    box-shadow: 0 12px 26px -14px rgba(139, 92, 246, 0.8);
    white-space: nowrap;
  }

  .notice.error {
    margin-bottom: 10px;
    padding: 11px 13px;
    border-radius: 13px;
    border: 1px solid rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
    font-size: 0.8rem;
  }

  .loading {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 18px 4px;
    color: var(--muted);
    font-size: 0.82rem;
  }

  .spinner {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.16);
    border-top-color: var(--accent-2);
    animation: spin 0.9s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .empty {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 20px;
    border-radius: 16px;
    border: 1px dashed var(--border);
    color: var(--muted);
  }

  .empty strong {
    color: var(--text);
    display: block;
    margin-bottom: 3px;
  }

  .empty p {
    margin: 0;
    font-size: 0.78rem;
    max-width: 58ch;
  }

  .wave-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .wave-note {
    margin: 10px 2px 0;
    color: var(--muted);
    font-size: 0.72rem;
    opacity: 0.85;
  }
</style>
