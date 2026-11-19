<script lang="ts">
  import { onMount } from 'svelte';
  import CoverArt from './CoverArt.svelte';
  import Icon from './Icon.svelte';
  import { api, ApiError } from '../lib/api';
  import {
    artistNames,
    availabilityLabel,
    formatDuration,
  } from '../lib/art';
  import { libraryVersion } from '../lib/stores';
  import type { Track } from '../lib/types';

  /**
   * P4 offline shelf: tracks whose local file the server has verified
   * (`GET /library/tracks?availability=offline`). Nothing is inferred on the
   * client: a track is listed only if the server returned it as `offline`.
   *
   * The shelf reloads after WS `library.updated` (the App bumps
   * `libraryVersion`), so a completed download appears without a page refresh.
   * When the library has no verified files the shelf stays honestly empty —
   * no sample rows, no fake "готово" (AGENTS.md §12).
   */
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: ((trackId: number) => void) | null = null;
  export let currentTrackId: number | null = null;
  export let busy = false;
  export let limit = 50;
  export let title = 'Офлайн-полка';
  export let subtitle = 'Треки с проверенным локальным файлом';

  let tracks: Track[] = [];
  let total = 0;
  let loading = false;
  let error = '';
  let requestSeq = 0;
  let disposed = false;
  let lastVersion = -1;

  $: countLabel =
    total > tracks.length ? `${tracks.length} из ${total}` : `${tracks.length}`;

  onMount(() => {
    void load();
    const unsubscribe = libraryVersion.subscribe((value) => {
      if (value === lastVersion) return;
      lastVersion = value;
      if (value > 0) void load();
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  });

  function describe(err: unknown): string {
    if (err instanceof ApiError) {
      return err.code === 'unavailable'
        ? 'Сервер временно не отдаёт медиатеку. Повторите позже.'
        : err.message;
    }
    return 'Не удалось загрузить офлайн-треки: сервер не отвечает.';
  }

  async function load(): Promise<void> {
    const token = ++requestSeq;
    loading = true;
    error = '';
    try {
      const page = await api.libraryTracks({ limit, offset: 0, availability: 'offline' });
      if (token !== requestSeq) return;
      // The server filter is authoritative; this guard only keeps a mislabelled
      // track from appearing on a shelf that promises offline playback.
      tracks = page.items.filter((track) => track.availability === 'offline');
      total = page.total;
    } catch (err) {
      if (token !== requestSeq) return;
      tracks = [];
      total = 0;
      error = describe(err);
    } finally {
      if (token === requestSeq) loading = false;
    }
  }
</script>

<section class="shelf material-content" aria-labelledby="offline-shelf-title">
  <div class="shelf-head">
    <div>
      <h2 id="offline-shelf-title">{title}</h2>
      <p class="shelf-sub">{subtitle}</p>
    </div>
    <div class="head-side">
      {#if !loading && !error && tracks.length > 0}
        <span class="count">{countLabel}</span>
      {/if}
      <button
        type="button"
        class="ghost small"
        disabled={loading}
        title="Перечитать офлайн-треки с сервера"
        onclick={() => void load()}
      >
        Обновить
      </button>
    </div>
  </div>

  {#if loading && tracks.length === 0}
    <div class="skeletons" aria-hidden="true">
      {#each [0, 1, 2] as key (key)}
        <div class="skeleton">
          <span class="skeleton-cover"></span>
          <span class="skeleton-main">
            <span class="skeleton-line"></span>
            <span class="skeleton-line short"></span>
          </span>
        </div>
      {/each}
    </div>
    <span class="sr-only" role="status">Загружаем офлайн-треки…</span>
  {:else if error}
    <div class="notice error" role="alert">
      <span>{error}</span>
      <button type="button" class="ghost small" onclick={() => void load()}>Повторить</button>
    </div>
  {:else if tracks.length === 0}
    <div class="empty">
      <Icon name="music" size={28} />
      <div>
        <strong>Офлайн-треков нет</strong>
        <p>
          Здесь появятся треки, для которых сервер подтвердил локальный файл:
          после завершения загрузки или импорта локальной медиатеки. Пока
          проверенных файлов нет — полка честно пуста.
        </p>
      </div>
    </div>
  {:else}
    <ul class="rows">
      {#each tracks as track (track.id)}
        <li class="row" class:playing={track.id === currentTrackId}>
          <button
            type="button"
            class="cover-button"
            disabled={busy}
            aria-label={`Играть «${track.title}»`}
            title="Играть"
            onclick={() => onPlay(track.id)}
          >
            <span class="cover">
              <CoverArt
                url={track.cover_url}
                title={track.title}
                alt={`Обложка «${track.title}»`}
                size={300}
              />
              <span class="play-mark" aria-hidden="true"><Icon name="play" size={14} /></span>
            </span>
          </button>

          <div class="main">
            <span class="title" title={track.title}>{track.title}</span>
            <span class="artists" title={artistNames(track.artists)}>{artistNames(track.artists)}</span>
            {#if track.album?.title}
              <span class="album" title={track.album.title}>{track.album.title}</span>
            {/if}
          </div>

          <span class="badge" title="Файл проверен сервером и доступен без сети">
            {availabilityLabel(track.availability)}
          </span>

          <span class="duration">{formatDuration(track.duration_ms)}</span>

          <div class="actions">
            {#if onEnqueue}
              <button
                type="button"
                class="action"
                disabled={busy}
                title="В очередь"
                aria-label={`Добавить «${track.title}» в очередь`}
                onclick={() => onEnqueue?.(track.id)}
              >
                <Icon name="plus" size={16} />
              </button>
            {/if}
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
    flex-wrap: wrap;
    margin-bottom: 8px;
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

  .head-side {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .count {
    color: var(--muted);
    font-size: 0.72rem;
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

  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .row {
    display: grid;
    grid-template-columns: 46px minmax(0, 1fr) auto auto auto;
    align-items: center;
    gap: 12px;
    padding: 8px 10px;
    border-radius: 14px;
    border: 1px solid transparent;
    transition:
      background 140ms ease,
      border-color 140ms ease;
  }

  .row:hover,
  .row:focus-within {
    background: rgba(255, 255, 255, 0.035);
    border-color: var(--border);
  }

  .row.playing {
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

  .badge {
    padding: 3px 9px;
    border-radius: 999px;
    border: 1px solid rgba(134, 239, 172, 0.28);
    background: rgba(16, 185, 129, 0.09);
    color: #86efac;
    font-size: 0.64rem;
    font-weight: 700;
    white-space: nowrap;
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
    justify-content: flex-end;
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
      color 120ms ease;
  }

  .action:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--border);
  }

  /* Touch devices: shelf controls meet the 44x44px target floor. */
  @media (pointer: coarse) {
    .action {
      width: 44px;
      height: 44px;
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

  .empty :global(.icon) {
    color: var(--accent-2);
    opacity: 0.75;
  }

  .empty strong {
    display: block;
    color: var(--text);
    margin-bottom: 3px;
  }

  .empty p {
    margin: 0;
    font-size: 0.78rem;
    line-height: 1.5;
    max-width: 68ch;
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
    font-size: 0.78rem;
  }

  .notice.error {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
  }

  .notice span {
    overflow-wrap: anywhere;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .skeleton {
    display: grid;
    grid-template-columns: 46px minmax(0, 1fr);
    gap: 12px;
    align-items: center;
    padding: 8px 10px;
  }

  .skeleton-cover,
  .skeleton-line {
    display: block;
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
    width: 46px;
    height: 46px;
    border-radius: 10px;
  }

  .skeleton-main {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .skeleton-line {
    height: 9px;
    border-radius: 999px;
  }

  .skeleton-line.short {
    width: 55%;
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

  @media (max-width: 720px) {
    .row {
      grid-template-columns: 42px minmax(0, 1fr) auto;
      row-gap: 6px;
    }
    .cover {
      width: 42px;
      height: 42px;
    }
    .badge {
      grid-column: 2;
    }
    .duration {
      display: none;
    }
    .actions {
      grid-column: 3;
      grid-row: 1 / span 2;
    }
    .action {
      width: 34px;
      height: 34px;
    }
    .shelf-head {
      align-items: flex-start;
    }
  }
</style>
