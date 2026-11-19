<script lang="ts">
  import Icon from './Icon.svelte';
  import CoverArt from './CoverArt.svelte';
  import { api } from '../lib/api';
  import { artistNames, formatDuration } from '../lib/art';
  import type { Track } from '../lib/types';

  /**
   * Detail page for one local album or artist. The App owns navigation and
   * playback; this component only loads and presents the track list.
   */
  export let kind: 'album' | 'artist';
  export let id: number;
  export let onBack: () => void = () => {};
  export let onPlay: (trackId: number) => void = () => {};
  export let onEnqueue: (trackId: number) => void = () => {};
  export let onPlayAll: (trackIds: number[]) => void = () => {};
  export let onEnqueueAll: (trackIds: number[]) => void = () => {};
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null =
    null;

  let loading = true;
  let error = '';
  let title = '';
  let subtitle = '';
  let coverUrl: string | null = null;
  let tracks: Track[] = [];

  $: trackIds = tracks.map((track) => track.id);

  async function load(): Promise<void> {
    loading = true;
    error = '';
    try {
      if (kind === 'album') {
        const detail = await api.libraryAlbum(id);
        title = detail.album.title;
        subtitle = [detail.album.artist?.name ?? null, detail.album.year ? String(detail.album.year) : null]
          .filter(Boolean)
          .join(' · ');
        coverUrl = detail.album.cover_url;
        tracks = detail.tracks;
      } else {
        const detail = await api.libraryArtist(id);
        title = detail.artist.name;
        subtitle =
          `${detail.artist.track_count} ${tracksWord(detail.artist.track_count)} · ` +
          `${detail.artist.album_count} ${albumsWord(detail.artist.album_count)}`;
        coverUrl = detail.artist.cover_url;
        tracks = detail.tracks;
      }
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Не удалось загрузить.';
      onToast?.(error, 'error');
    } finally {
      loading = false;
    }
  }

  // Reload when the target changes (e.g. album → artist without unmount).
  $: loadKey = `${kind}:${id}`;
  $: loadKey, void load();

  function tracksWord(count: number): string {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return 'трек';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'трека';
    return 'треков';
  }

  function albumsWord(count: number): string {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return 'альбом';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'альбома';
    return 'альбомов';
  }
</script>

<section class="detail material-content" aria-labelledby="library-detail-title">
  <header class="detail-head">
    <button type="button" class="back" onclick={onBack}>
      <Icon name="chevron-left" size={16} /> Медиатека
    </button>
    <div class="head-main">
      <div class="cover">
        {#if coverUrl}
          <CoverArt url={coverUrl} title={title} size={300} />
        {:else}
          <Icon name={kind === 'artist' ? 'music' : 'queue'} size={28} />
        {/if}
      </div>
      <div class="head-copy">
        <span class="eyebrow">{kind === 'album' ? 'Медиатека · альбом' : 'Медиатека · исполнитель'}</span>
        <h1 id="library-detail-title">{title || (loading ? 'Загрузка…' : 'Не найдено')}</h1>
        {#if subtitle}<p class="sub">{subtitle}</p>{/if}
      </div>
    </div>
    <div class="head-actions">
      <button
        type="button"
        class="primary"
        disabled={trackIds.length === 0}
        onclick={() => onPlayAll(trackIds)}
      >
        <Icon name="play" size={15} /> Слушать
      </button>
      <button
        type="button"
        class="ghost"
        disabled={trackIds.length === 0}
        onclick={() => onEnqueueAll(trackIds)}
      >
        <Icon name="queue" size={15} /> В очередь
      </button>
    </div>
  </header>

  {#if loading}
    <div class="state" aria-live="polite">Загрузка…</div>
  {:else if error}
    <div class="notice error" role="alert">
      <strong>Не удалось загрузить.</strong>
      <span>{error}</span>
      <button type="button" class="ghost small" onclick={() => void load()}>Повторить</button>
    </div>
  {:else if tracks.length === 0}
    <div class="empty">
      <strong>Треков нет</strong>
      <p>
        У этого {kind === 'album' ? 'альбома' : 'исполнителя'} пока нет доступных
        треков в библиотеке.
      </p>
    </div>
  {:else}
    <ul class="track-list">
      {#each tracks as track, index (track.id)}
        <li>
          <button
            type="button"
            class="row-main"
            aria-label={`Играть «${track.title}»`}
            onclick={() => onPlay(track.id)}
          >
            <span class="row-index" aria-hidden="true">{index + 1}</span>
            <span class="row-copy">
              <span class="row-title">{track.title}</span>
              <span class="row-sub">
                {kind === 'artist'
                  ? track.album?.title ?? artistNames(track.artists)
                  : artistNames(track.artists)}
              </span>
            </span>
            <span class="row-duration">{formatDuration(track.duration_ms)}</span>
          </button>
          <button
            type="button"
            class="tool"
            aria-label={`Добавить «${track.title}» в очередь`}
            onclick={() => onEnqueue(track.id)}
          >
            <Icon name="queue" size={15} />
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .detail-head {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 22px;
  }

  .back {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 7px 13px 7px 9px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-soft);
    font-size: var(--text-meta);
    font-weight: 600;
  }

  .back:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .head-main {
    display: flex;
    align-items: flex-end;
    gap: 20px;
    flex-wrap: wrap;
  }

  .cover {
    display: grid;
    place-items: center;
    width: 148px;
    height: 148px;
    flex: none;
    border: 1px solid var(--stroke);
    border-radius: 18px;
    background: var(--surface);
    color: var(--text-muted);
    overflow: hidden;
  }

  .head-copy { min-width: 0; }

  .eyebrow {
    display: block;
    margin-bottom: 5px;
    color: var(--accent-2);
    font-size: var(--text-micro);
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: var(--text-page);
    font-weight: 700;
    line-height: 1.08;
    letter-spacing: -0.03em;
    overflow-wrap: anywhere;
  }

  .sub {
    max-width: 60ch;
    margin: 8px 0 0;
    color: var(--text-soft);
    font-size: var(--text-body);
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .primary {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 9px 17px;
    border: 0;
    border-radius: var(--radius-pill);
    background: var(--accent);
    color: #070b13;
    font-size: var(--text-body);
    font-weight: 700;
  }

  .primary:hover:not(:disabled) {
    background: color-mix(in srgb, var(--accent) 88%, white);
  }

  .ghost {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 9px 17px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-soft);
    font-size: var(--text-body);
    font-weight: 600;
  }

  .ghost:hover:not(:disabled),
  .ghost.small {
    background: var(--surface-hover);
  }

  .state { color: var(--text-muted); }

  .notice.error {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    padding: 16px;
    border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--danger) 8%, transparent);
    color: #fecdd3;
  }

  .empty {
    padding: 22px;
    border: 1px dashed var(--border);
    border-radius: 18px;
    color: var(--muted);
  }

  .empty strong { display: block; color: var(--text); margin-bottom: 2px; }
  .empty p { margin: 0; font-size: 0.8rem; }

  .track-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .track-list li {
    display: flex;
    align-items: stretch;
    gap: 2px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }

  .row-main {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 14px;
    min-width: 0;
    padding: 10px 10px;
    border-radius: var(--radius-sm);
    color: var(--text);
    text-align: left;
  }

  .row-main:hover { background: var(--surface-hover); }

  .row-index {
    min-width: 22px;
    color: var(--text-muted);
    font-size: var(--text-meta);
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  .row-copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }

  .row-title {
    font-size: var(--text-card);
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-sub {
    color: var(--text-muted);
    font-size: var(--text-meta);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-duration {
    margin-left: auto;
    color: var(--text-muted);
    font-size: var(--text-meta);
    font-variant-numeric: tabular-nums;
  }

  .tool {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 9px;
    color: var(--muted);
  }

  .tool:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  @media (pointer: coarse) {
    .tool { width: 44px; height: 44px; }
  }
</style>
