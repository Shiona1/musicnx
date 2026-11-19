<script lang="ts">
  import Icon from './Icon.svelte';
  import DownloadButton from './DownloadButton.svelte';
  import {
    artistNames,
    availabilityLabel,
    coverBackground,
    coverInitials,
    formatDuration,
  } from '../lib/art';
  import type { YandexTrack } from '../lib/yandex';

  export let title: string;
  export let subtitle = '';
  export let tracks: YandexTrack[] = [];
  export let total = 0;
  export let loading = false;
  export let error = '';
  export let emptyTitle = 'Пока пусто';
  export let emptyHint = '';
  export let currentTrackId: number | null = null;
  export let busy = false;
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: (trackId: number) => void;
</script>

<section class="shelf material-content" aria-label={title}>
  <div class="shelf-head">
    <div>
      <h2>{title}</h2>
      {#if subtitle}<p class="shelf-sub">{subtitle}</p>{/if}
    </div>
    {#if !loading && tracks.length > 0}
      <span class="count">
        {total > tracks.length ? `${tracks.length} из ${total}` : `${tracks.length}`}
      </span>
    {/if}
  </div>

  {#if loading}
    <div class="skeletons" aria-hidden="true">
      {#each [0, 1, 2, 3, 4] as key (key)}
        <div class="skeleton">
          <span class="skeleton-cover"></span>
          <span class="skeleton-line"></span>
          <span class="skeleton-line short"></span>
        </div>
      {/each}
    </div>
    <span class="sr-loading" role="status">Загрузка треков…</span>
  {:else if error}
    <div class="notice error" role="alert">{error}</div>
  {:else if tracks.length === 0}
    <div class="empty">
      <Icon name="music" size={28} />
      <div>
        <strong>{emptyTitle}</strong>
        {#if emptyHint}<p>{emptyHint}</p>{/if}
      </div>
    </div>
  {:else}
    <ul class="cards">
      {#each tracks as track (track.id)}
        <li class="card" class:active={track.id === currentTrackId}>
          <button
            class="cover"
            type="button"
            style={coverBackground(track.cover_url, `${track.title}|${track.album?.title ?? ''}`)}
            aria-label={`Воспроизвести «${track.title}»`}
            disabled={busy}
            onclick={() => onPlay(track.id)}
          >
            {#if !track.cover_url}
              <span class="initials" aria-hidden="true">{coverInitials(track.title)}</span>
            {/if}
            {#if track.preview}
              <span class="preview-badge" title="Превью: звучит 30 секунд">30 сек</span>
            {/if}
            <span class="cover-play" aria-hidden="true"><Icon name="play" size={18} /></span>
            {#if track.id === currentTrackId}
              <span class="live" aria-label="Сейчас играет">
                <span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>
              </span>
            {/if}
          </button>

          <div class="meta">
            <span class="title" title={track.title}>{track.title}</span>
            <span class="artist" title={artistNames(track.artists)}>{artistNames(track.artists)}</span>
            <div class="bottom">
              <span class="availability" class:ok={track.availability === 'online' || track.availability === 'offline'}>
                {availabilityLabel(track.availability)}
              </span>
              <span class="duration">{formatDuration(track.duration_ms)}</span>
              <DownloadButton compact trackId={track.id} disabled={busy} />
              <button
                class="enqueue"
                type="button"
                aria-label={`Добавить «${track.title}» в очередь`}
                title="В очередь"
                disabled={busy}
                onclick={() => onEnqueue(track.id)}
              >
                <Icon name="plus" size={16} />
              </button>
            </div>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .shelf {
    border-radius: 22px;
    padding: clamp(16px, 2vw, 22px);
  }

  .shelf-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 14px;
  }

  h2 {
    margin: 0;
    font-size: 1rem;
    letter-spacing: 0.01em;
  }

  .shelf-sub {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 0.74rem;
  }

  .count {
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .cards {
    list-style: none;
    margin: 0;
    padding: 2px 2px 8px;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(140px, 168px);
    gap: 14px;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    scrollbar-width: thin;
  }

  .card {
    scroll-snap-align: start;
    min-width: 0;
    border-radius: 16px;
    padding: 8px;
    border: 1px solid transparent;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      transform 140ms ease;
  }

  .card:hover,
  .card:focus-within {
    background: rgba(255, 255, 255, 0.035);
    border-color: var(--border);
  }

  .card.active {
    border-color: rgba(34, 211, 238, 0.35);
    background: rgba(34, 211, 238, 0.06);
  }

  .cover {
    position: relative;
    display: block;
    width: 100%;
    aspect-ratio: 1;
    background-size: cover;
    background-position: center;
    border-radius: 13px;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.85);
    padding: 0;
    text-align: center;
    transition: transform 160ms ease;
  }

  .card:hover .cover:not(:disabled) {
    transform: translateY(-2px);
  }

  .cover::after {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at 30% 20%, rgba(255, 255, 255, 0.15), transparent 60%);
  }

  .initials {
    position: relative;
    z-index: 1;
    font-size: 1.5rem;
    font-weight: 800;
    letter-spacing: 0.05em;
  }

  .cover-play {
    position: absolute;
    z-index: 2;
    right: 8px;
    bottom: 8px;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: rgba(5, 8, 14, 0.62);
    opacity: 0;
    transform: translateY(4px);
    transition:
      opacity 140ms ease,
      transform 140ms ease;
  }

  .cover:hover .cover-play,
  .cover:focus-visible .cover-play {
    opacity: 1;
    transform: translateY(0);
  }

  .preview-badge {
    position: absolute;
    z-index: 2;
    left: 7px;
    top: 7px;
    padding: 3px 7px;
    border-radius: 999px;
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    color: #fde68a;
    background: rgba(5, 8, 14, 0.66);
    border: 1px solid rgba(253, 230, 138, 0.35);
  }

  .live {
    position: absolute;
    z-index: 2;
    right: 8px;
    top: 8px;
    display: inline-flex;
    padding: 5px 7px;
    border-radius: 999px;
    background: rgba(5, 8, 14, 0.6);
  }

  .eq {
    display: inline-flex;
    align-items: flex-end;
    gap: 2px;
    height: 9px;
  }

  .eq i {
    width: 2px;
    height: 100%;
    background: var(--accent-2);
    animation: bounce 0.9s ease-in-out infinite;
  }

  .eq i:nth-child(2) {
    animation-delay: 0.15s;
  }
  .eq i:nth-child(3) {
    animation-delay: 0.3s;
  }

  @keyframes bounce {
    0%,
    100% {
      transform: scaleY(0.35);
    }
    50% {
      transform: scaleY(1);
    }
  }

  .meta {
    padding: 9px 2px 2px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }

  .title {
    font-size: 0.83rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .artist {
    color: var(--muted);
    font-size: 0.73rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .bottom {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 2px;
  }

  .availability {
    font-size: 0.62rem;
    color: #fca5a5;
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .availability.ok {
    color: #86efac;
  }

  .duration {
    color: var(--muted);
    font-size: 0.66rem;
    font-variant-numeric: tabular-nums;
    margin-left: auto;
  }

  .enqueue {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: 8px;
    color: var(--muted);
    border: 1px solid transparent;
  }

  .enqueue:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--border);
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
    margin-bottom: 2px;
  }

  .empty p {
    margin: 0;
    font-size: 0.78rem;
  }

  .notice.error {
    padding: 12px 14px;
    border-radius: 14px;
    border: 1px solid rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
    font-size: 0.8rem;
  }

  .skeletons {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(140px, 168px);
    gap: 14px;
    overflow: hidden;
  }

  .skeleton {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 8px;
  }

  .skeleton-cover,
  .skeleton-line {
    display: block;
    border-radius: 12px;
    background: linear-gradient(
      100deg,
      rgba(255, 255, 255, 0.06) 25%,
      rgba(255, 255, 255, 0.12) 45%,
      rgba(255, 255, 255, 0.06) 65%
    );
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .skeleton-cover {
    aspect-ratio: 1;
  }

  .skeleton-line {
    height: 9px;
    border-radius: 999px;
  }

  .skeleton-line.short {
    width: 60%;
  }

  @keyframes shimmer {
    0% {
      background-position: 120% 0;
    }
    100% {
      background-position: -40% 0;
    }
  }

  .sr-loading {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
