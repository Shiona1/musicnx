<script lang="ts">
  import CoverArt from './CoverArt.svelte';
  import Icon from './Icon.svelte';
  import DownloadButton from './DownloadButton.svelte';
  import { artistNames, formatDuration } from '../lib/art';
  import type { WaveAction, YandexTrack } from '../lib/yandex';

  export let track: YandexTrack;
  export let index: number | null = null;
  export let current = false;
  export let busy = false;
  export let feedbackBusy = false;
  export let showAlbum = true;
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: ((trackId: number) => void) | null = null;
  export let onFeedback: ((action: WaveAction) => void) | null = null;
  export let onRemove: ((position: number) => void) | null = null;

  $: rateDisabled = busy || feedbackBusy;
</script>

<li class="track-row" class:current class:busy={rateDisabled}>
  <button
    class="cover-button"
    type="button"
    aria-label={`Воспроизвести «${track.title}»`}
    disabled={busy}
    onclick={() => onPlay(track.id)}
  >
    <span class="cover">
      <CoverArt url={track.cover_url} title={track.title} size={100} />
      <span class="play-mark" aria-hidden="true"><Icon name="play" size={14} /></span>
    </span>
  </button>

  <div class="main">
    <span class="title" title={track.title}>{track.title}</span>
    <span class="artists" title={artistNames(track.artists)}>{artistNames(track.artists)}</span>
    {#if showAlbum && track.album?.title}
      <span class="album" title={track.album.title}>{track.album.title}</span>
    {/if}
  </div>

  <div class="badges">
    {#if index !== null}
      <span class="index" aria-hidden="true">{index + 1}</span>
    {/if}
    {#if track.preview}
      <span class="chip preview" title="Превью: звучит 30 секунд">30 сек</span>
    {/if}
    {#if track.liked === true}
      <span class="chip liked" title="Трек в «Мне нравится»">лайк</span>
    {/if}
  </div>

  <span class="duration">{formatDuration(track.duration_ms)}</span>

  <div class="actions">
    {#if onEnqueue}
      <button
        class="action"
        type="button"
        aria-label={`Добавить «${track.title}» в очередь`}
        title="В очередь"
        disabled={busy}
        onclick={() => onEnqueue?.(track.id)}
      >
        <Icon name="plus" size={16} />
      </button>
    {/if}

    <DownloadButton compact trackId={track.id} disabled={busy} />

    {#if onRemove && index !== null}
      <button
        class="action"
        type="button"
        aria-label={`Убрать «${track.title}» из плейлиста`}
        title="Убрать из плейлиста"
        disabled={busy}
        onclick={() => onRemove?.(index)}
      >
        <Icon name="close" size={15} />
      </button>
    {/if}

    {#if onFeedback}
      <button
        class="action rate"
        type="button"
        aria-label={`Мне нравится «${track.title}»`}
        title="Нравится"
        disabled={rateDisabled}
        onclick={() => onFeedback?.('like')}
      >
        <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
          <path
            fill="currentColor"
            d="M1 21h4V9H1v12zM23 10c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"
          />
        </svg>
      </button>
      <button
        class="action rate"
        type="button"
        aria-label={`Не нравится «${track.title}»`}
        title="Не нравится"
        disabled={rateDisabled}
        onclick={() => onFeedback?.('dislike')}
      >
        <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
          <path
            fill="currentColor"
            d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"
          />
        </svg>
      </button>
      <button
        class="action"
        type="button"
        aria-label={`Пропустить «${track.title}»`}
        title="Пропустить и не играть в волне"
        disabled={rateDisabled}
        onclick={() => onFeedback?.('skip')}
      >
        <Icon name="next" size={16} />
      </button>
    {/if}
  </div>
</li>

<style>
  .track-row {
    display: grid;
    grid-template-columns: 46px minmax(0, 1fr) auto auto auto;
    align-items: center;
    gap: 12px;
    padding: 8px 10px;
    border-radius: 14px;
    border: 1px solid transparent;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      transform 140ms ease;
  }

  .track-row:hover,
  .track-row:focus-within {
    background: rgba(255, 255, 255, 0.035);
    border-color: var(--border);
  }

  .track-row.current {
    border-color: rgba(34, 211, 238, 0.32);
    background: rgba(34, 211, 238, 0.06);
  }

  .cover-button {
    display: block;
    padding: 0;
    border-radius: 10px;
    transition: transform 140ms ease;
  }

  .cover-button:hover:not(:disabled) {
    transform: translateY(-1px);
  }

  .cover {
    position: relative;
    display: grid;
    place-items: center;
    width: 46px;
    height: 46px;
    border-radius: 10px;
    background-size: cover;
    background-position: center;
    border: 1px solid rgba(255, 255, 255, 0.12);
    overflow: hidden;
    color: rgba(255, 255, 255, 0.85);
  }

  .play-mark {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    background: rgba(5, 8, 14, 0.55);
    opacity: 0;
    transition: opacity 140ms ease;
  }

  .cover-button:hover .play-mark,
  .cover-button:focus-visible .play-mark {
    opacity: 1;
  }

  .main {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
  }

  .title {
    font-size: 0.86rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .artists,
  .album {
    color: var(--muted);
    font-size: 0.74rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .album {
    opacity: 0.75;
  }

  .badges {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .index {
    color: var(--muted);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    min-width: 1.4em;
    text-align: right;
  }

  .chip {
    padding: 2px 7px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.64rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  .chip.preview {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.32);
    background: rgba(252, 211, 77, 0.1);
  }

  .chip.liked {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.32);
    background: rgba(139, 92, 246, 0.1);
  }

  .duration {
    color: var(--muted);
    font-size: 0.74rem;
    font-variant-numeric: tabular-nums;
    min-width: 3.2em;
    text-align: right;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 2px;
  }

  .action {
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 9px;
    border: 1px solid transparent;
    color: var(--muted);
    transition:
      background 120ms ease,
      color 120ms ease,
      transform 120ms ease;
  }

  /* Touch devices: row actions meet the 44x44px target floor. */
  @media (pointer: coarse) {
    .action {
      width: 44px;
      height: 44px;
    }
  }

  .action:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--border);
    transform: translateY(-1px);
  }

  .action.rate:hover:not(:disabled) {
    color: var(--accent-2);
  }

  @media (max-width: 720px) {
    .track-row {
      grid-template-columns: 42px minmax(0, 1fr) auto;
      row-gap: 6px;
    }
    .cover {
      width: 42px;
      height: 42px;
    }
    .badges {
      grid-column: 2;
      justify-content: flex-start;
    }
    .duration {
      display: none;
    }
    .actions {
      grid-column: 3;
      grid-row: 1 / span 2;
      flex-direction: column;
    }
    .action {
      width: 34px;
      height: 34px;
    }
  }
</style>
