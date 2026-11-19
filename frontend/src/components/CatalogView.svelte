<script lang="ts">
  import { onDestroy } from 'svelte';
  import Icon from './Icon.svelte';
  import CoverArt from './CoverArt.svelte';
  import DownloadButton from './DownloadButton.svelte';
  import YandexTrackRow from './YandexTrackRow.svelte';
  import { yandexApi } from '../lib/yandex';
  import { albumsWord, artistsWord, tracksWord } from '../lib/art';
  import type {
    AlbumDetail,
    ArtistDetail,
    PlaylistDetail,
    YandexSearchResults,
    YandexTrack,
  } from '../lib/yandex';
  import type { CatalogTarget } from '../lib/routes';

  /**
   * Playlist / album / artist / search page for Yandex Music.
   *
   * The backend has exposed these catalog endpoints since P3; this view is the
   * missing UI. It never invents a section: loading and empty states come from
   * the server response, and a failed request leaves an explicit error.
   */
  export let target: CatalogTarget;
  export let currentTrackId: number | null = null;
  export let busy = false;
  export let onBack: () => void = () => {};
  export let onPlay: (trackId: number) => void = () => {};
  export let onEnqueue: (trackId: number) => void = () => {};
  export let onEnqueueMany: (trackIds: number[]) => void = () => {};
  export let onPlayCollection: ((trackIds: number[]) => void) | null = null;
  export let onOpenPlaylist: (playlistId: string) => void = () => {};
  export let onOpenAlbum: (albumId: string) => void = () => {};
  export let onOpenArtist: (artistId: string) => void = () => {};
  export let onToast: ((message: string, tone?: 'info' | 'error') => void) | null = null;

  let loading = true;
  let error = '';
  let playlist: PlaylistDetail['playlist'] | null = null;
  let album: AlbumDetail['album'] | null = null;
  let artist: ArtistDetail['artist'] | null = null;
  let tracks: YandexTrack[] = [];
  let search: YandexSearchResults | null = null;
  let lastKey = '';
  let searchLoadingMore = false;
  let searchOffset = 0;
  let artistTotal = 0;
  let artistLoadingMore = false;

  $: key = `${target.kind}:${target.id ?? ''}:${target.query ?? ''}`;
  $: if (key !== lastKey) {
    lastKey = key;
    void load();
  }

  // Live search: the search page re-queries as you type. Requests are
  // debounced (600 ms, at least two characters — PAG-01 asks not to spam
  // Yandex) and serialized; a failed keystroke keeps the already shown
  // results instead of wiping the page.
  let searchInput = '';
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let searchSeq = 0;

  $: if (target.kind === 'search') searchInput = (target.query ?? '').trim();

  function scheduleLiveSearch(): void {
    if (searchTimer !== null) clearTimeout(searchTimer);
    const query = searchInput.trim();
    if (query.length < 2) return;
    searchTimer = setTimeout(() => void runLiveSearch(query), 600);
  }

  function submitLiveSearch(event: SubmitEvent): void {
    event.preventDefault();
    if (searchTimer !== null) clearTimeout(searchTimer);
    const query = searchInput.trim();
    if (query.length < 2) return;
    void runLiveSearch(query);
  }

  async function runLiveSearch(query: string): Promise<void> {
    const seq = ++searchSeq;
    loading = true;
    try {
      const next = await yandexApi.search({ q: query, type: 'all', limit: 50 });
      if (seq !== searchSeq || target.kind !== 'search') return;
      search = next;
      searchOffset = 50;
      tracks = next.tracks.items;
      error = '';
    } catch (cause) {
      if (seq === searchSeq) {
        onToast?.(
          cause instanceof Error ? cause.message : 'Не удалось выполнить поиск.',
          'error',
        );
      }
    } finally {
      if (seq === searchSeq) loading = false;
    }
  }

  onDestroy(() => {
    if (searchTimer !== null) clearTimeout(searchTimer);
  });

  async function load(): Promise<void> {
    loading = true;
    error = '';
    playlist = null;
    album = null;
    artist = null;
    search = null;
    tracks = [];
    try {
      if (target.kind === 'playlist' && target.id) {
        const detail = await yandexApi.playlist(target.id, { limit: 200 });
        playlist = detail.playlist;
        let items = detail.tracks.items;
        let total = detail.tracks.total;
        // A playlist can be longer than one page; keep fetching until the full
        // list is loaded so «Слушать»/«В очередь» really cover it.
        let guard = 0;
        while (items.length < total && guard < 10) {
          guard += 1;
          const page = await yandexApi.playlist(target.id, {
            limit: 200,
            offset: items.length,
          });
          if (page.tracks.items.length === 0) break;
          items = items.concat(page.tracks.items);
          total = page.tracks.total;
        }
        tracks = items;
      } else if (target.kind === 'album' && target.id) {
        const detail = await yandexApi.album(target.id);
        album = detail.album;
        tracks = detail.tracks;
      } else if (target.kind === 'artist' && target.id) {
        const detail = await yandexApi.artist(target.id);
        artist = detail.artist;
        try {
          const page = await yandexApi.artistTracks(target.id, { limit: 25, offset: 0 });
          tracks = page.items;
          artistTotal = page.total;
        } catch {
          tracks = detail.tracks;
          artistTotal = detail.tracks.length;
        }
      } else if (target.kind === 'search') {
        const query = (target.query ?? '').trim();
        if (!query) {
          error = 'Введите поисковый запрос.';
          return;
        }
        search = await yandexApi.search({ q: query, type: 'all', limit: 50 });
        searchOffset = 50;
        tracks = search.tracks.items;
      } else {
        error = 'Некорректная ссылка каталога.';
      }
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Не удалось загрузить данные.';
    } finally {
      loading = false;
    }
  }

  $: title =
    target.kind === 'search'
      ? 'Поиск'
      : playlist?.title ??
        album?.title ??
        artist?.name ??
        (loading ? 'Загрузка…' : 'Не найдено');

  $: subtitle =
    target.kind === 'playlist'
      ? [
          playlist?.owner && !/^\d+$/.test(playlist.owner) ? playlist.owner : null,
          playlist?.track_count !== null && playlist?.track_count !== undefined
            ? `${playlist.track_count} ${tracksWord(playlist.track_count)}`
            : null,
        ]
          .filter(Boolean)
          .join(' · ')
      : target.kind === 'album'
        ? [
            album?.artist?.name,
            album?.year,
            album?.track_count ? `${album.track_count} ${tracksWord(album.track_count)}` : null,
          ]
            .filter(Boolean)
            .join(' · ')
        : target.kind === 'artist'
          ? [
              artist?.track_count ? `${artist.track_count} ${tracksWord(artist.track_count)}` : null,
              artist?.album_count ? `${artist.album_count} ${albumsWord(artist.album_count)}` : null,
            ]
              .filter(Boolean)
              .join(' · ')
          : `${search?.tracks.total ?? 0} ${tracksWord(search?.tracks.total ?? 0)}, ${search?.albums.total ?? 0} ${albumsWord(search?.albums.total ?? 0)}, ${search?.artists.total ?? 0} ${artistsWord(search?.artists.total ?? 0)}`;

  $: cover =
    playlist?.cover_url ??
    album?.cover_url ??
    artist?.cover_url ??
    null;

  $: firstTrackId = tracks.length > 0 ? tracks[0].id : null;
  $: allTrackIds = tracks.map((track) => track.id).filter((id) => id > 0);
  $: searchHasMore =
    target.kind === 'search' &&
    search !== null &&
    (search.tracks.items.length < search.tracks.total ||
      search.albums.items.length < search.albums.total ||
      search.artists.items.length < search.artists.total ||
      search.playlists.items.length < search.playlists.total);
  $: artistHasMore = target.kind === 'artist' && tracks.length < artistTotal;

  async function loadMoreArtist(): Promise<void> {
    if (!target.id || target.kind !== 'artist' || artistLoadingMore) return;
    artistLoadingMore = true;
    try {
      const page = await yandexApi.artistTracks(target.id, {
        limit: 25,
        offset: tracks.length,
      });
      const seen = new Set(tracks.map((item) => item.id));
      tracks = [...tracks, ...page.items.filter((item) => !seen.has(item.id))];
      artistTotal = page.total;
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось загрузить ещё.');
    } finally {
      artistLoadingMore = false;
    }
  }

  async function loadMoreSearch(): Promise<void> {
    if (!search || target.kind !== 'search' || searchLoadingMore) return;
    const query = searchInput.trim() || (target.query ?? '').trim();
    if (!query) return;
    searchLoadingMore = true;
    try {
      const offset = searchOffset;
      const next = await yandexApi.search({ q: query, type: 'all', limit: 50, offset });
      searchOffset += 50;
      const seenTracks = new Set(search.tracks.items.map((item) => item.id));
      const seenAlbums = new Set(search.albums.items.map((item) => item.id));
      const seenArtists = new Set(search.artists.items.map((item) => item.id));
      const seenPlaylists = new Set(search.playlists.items.map((item) => item.id));
      search = {
        tracks: {
          ...search.tracks,
          items: [
            ...search.tracks.items,
            ...next.tracks.items.filter((item) => !seenTracks.has(item.id)),
          ],
        },
        albums: {
          ...search.albums,
          items: [
            ...search.albums.items,
            ...next.albums.items.filter((item) => !seenAlbums.has(item.id)),
          ],
        },
        artists: {
          ...search.artists,
          items: [
            ...search.artists.items,
            ...next.artists.items.filter((item) => !seenArtists.has(item.id)),
          ],
        },
        playlists: {
          ...search.playlists,
          items: [
            ...search.playlists.items,
            ...next.playlists.items.filter((item) => !seenPlaylists.has(item.id)),
          ],
        },
      };
      tracks = search.tracks.items;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Не удалось загрузить ещё.';
    } finally {
      searchLoadingMore = false;
    }
  }

  function playAll(): void {
    if (allTrackIds.length === 0) return;
    if (onPlayCollection) {
      onPlayCollection(allTrackIds);
      return;
    }
    if (firstTrackId !== null) onPlay(firstTrackId);
  }

  function enqueueAll(): void {
    if (allTrackIds.length > 0) {
      onEnqueueMany(allTrackIds);
      onToast?.(`В очередь добавлено: ${allTrackIds.length}.`);
    }
  }

  async function renamePlaylist(): Promise<void> {
    if (!playlist || !target.id) return;
    const title = window.prompt('Новое название плейлиста', playlist.title);
    if (!title || !title.trim() || title.trim() === playlist.title) return;
    try {
      playlist = await yandexApi.renamePlaylist(target.id, title.trim());
      onToast?.(`Плейлист переименован: «${playlist.title}».`);
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось переименовать плейлист.');
    }
  }

  async function deletePlaylist(): Promise<void> {
    if (!playlist || !target.id) return;
    if (!window.confirm(`Удалить плейлист «${playlist.title}» в Яндекс Музыке?`)) return;
    try {
      await yandexApi.deletePlaylist(target.id);
      onToast?.('Плейлист удалён.');
      onBack();
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось удалить плейлист.');
    }
  }

  async function addCurrentTrack(): Promise<void> {
    if (!target.id || currentTrackId === null) return;
    try {
      await yandexApi.addPlaylistTrack(target.id, currentTrackId, tracks.length);
      onToast?.('Трек добавлен в плейлист.');
      await load();
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось добавить трек.');
    }
  }

  async function removePlaylistTrack(position: number): Promise<void> {
    if (target.kind !== 'playlist' || !target.id) return;
    try {
      await yandexApi.removePlaylistTrack(target.id, position);
      onToast?.('Трек убран из плейлиста.');
      await load();
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось убрать трек.');
    }
  }
</script>

<section class="catalog material-content" aria-label={`Каталог: ${title}`}>
  <header class="catalog-head">
    <button type="button" class="back" onclick={onBack}>
      <Icon name="chevron-left" size={16} /> Назад
    </button>
    <div class="head-main">
      {#if target.kind !== 'search'}
        <div class="cover" style={cover ? `background-image: url('${cover}')` : ''}>
          {#if !cover}
            <Icon name={target.kind === 'artist' ? 'music' : 'queue'} size={28} />
          {:else}
            <CoverArt url={cover} title={title} size={300} />
          {/if}
        </div>
      {/if}
      <div class="head-copy">
        <span class="eyebrow">{target.kind === 'search' ? 'Яндекс Музыка' : 'Яндекс Музыка · каталог'}</span>
        <h1>{title}</h1>
        {#if target.kind === 'search'}
          <form class="live-search" role="search" onsubmit={submitLiveSearch}>
            <input
              type="search"
              bind:value={searchInput}
              oninput={scheduleLiveSearch}
              placeholder="Что искать?…"
              aria-label="Поиск в Яндекс Музыке"
            />
          </form>
        {/if}
        {#if subtitle}<p class="sub">{subtitle}</p>{/if}
      </div>
    </div>
    <div class="head-actions">
      <button type="button" class="primary" disabled={!firstTrackId || busy} onclick={playAll}>
        <Icon name="play" size={15} /> Слушать
      </button>
      <button type="button" class="ghost" disabled={allTrackIds.length === 0 || busy} onclick={enqueueAll}>
        <Icon name="queue" size={15} /> В очередь
      </button>
      {#if target.kind === 'playlist' && target.id}
        <button type="button" class="ghost" disabled={busy} onclick={() => void addCurrentTrack()}>
          <Icon name="plus" size={15} /> Текущий трек
        </button>
        <button type="button" class="ghost" onclick={() => void renamePlaylist()}>
          Переименовать
        </button>
        <button type="button" class="ghost danger" onclick={() => void deletePlaylist()}>
          Удалить
        </button>
        <DownloadButton playlistId={target.id} label="Скачать плейлист" {onToast} disabled={busy} />
      {:else if target.kind === 'album' && album}
        <DownloadButton albumId={album.id} label="Скачать альбом" {onToast} disabled={busy} />
      {/if}
    </div>
  </header>

  {#if loading}
    <div class="skeletons" aria-hidden="true">
      {#each [0, 1, 2, 3, 4, 5] as key (key)}
        <div class="skeleton-row"></div>
      {/each}
    </div>
    <span class="sr-loading" role="status">Загрузка каталога…</span>
  {:else if error}
    <div class="notice error" role="alert">
      <strong>Не удалось загрузить.</strong>
      <span>{error}</span>
      <button type="button" class="ghost small" onclick={() => void load()}>Повторить</button>
    </div>
  {:else if target.kind === 'search' && search}
    <div class="search-sections">
      <section class="search-block" aria-labelledby="search-tracks">
        <div class="block-head">
          <h2 id="search-tracks">Треки</h2>
          <span class="count">{search.tracks.total}</span>
        </div>
        {#if tracks.length === 0}
          <p class="muted">Треков не найдено.</p>
        {:else}
          <ol class="track-list">
            {#each tracks as track, index (track.id)}
              <YandexTrackRow
                {track}
                {index}
                current={currentTrackId === track.id}
                {busy}
                {onPlay}
                {onEnqueue}
              />
            {/each}
          </ol>
        {/if}
      </section>

      {#if search.albums.items.length > 0}
        <section class="search-block" aria-labelledby="search-albums">
          <div class="block-head"><h2 id="search-albums">Альбомы</h2><span class="count">{search.albums.total}</span></div>
          <ul class="card-grid">
            {#each search.albums.items as item (item.id)}
              <li>
                <button type="button" class="mini-card" onclick={() => onOpenAlbum(String(item.id))}>
                  <span class="mini-cover">
                    <CoverArt url={item.cover_url} title={item.title} size={100} />
                  </span>
                  <strong>{item.title}</strong>
                  <span>{item.artist?.name ?? '—'} {item.year ? `· ${item.year}` : ''}</span>
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if search.artists.items.length > 0}
        <section class="search-block" aria-labelledby="search-artists">
          <div class="block-head"><h2 id="search-artists">Исполнители</h2><span class="count">{search.artists.total}</span></div>
          <ul class="card-grid">
            {#each search.artists.items as item (item.id)}
              <li>
                <button type="button" class="mini-card" onclick={() => onOpenArtist(String(item.id))}>
                  <span class="mini-cover round">
                    <CoverArt url={item.cover_url} title={item.name} size={100} />
                  </span>
                  <strong>{item.name}</strong>
                  <span>{item.track_count > 0 ? `${item.track_count} ${tracksWord(item.track_count)}` : 'исполнитель'}</span>
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if search.playlists.items.length > 0}
        <section class="search-block" aria-labelledby="search-playlists">
          <div class="block-head"><h2 id="search-playlists">Плейлисты</h2><span class="count">{search.playlists.total}</span></div>
          <ul class="card-grid">
            {#each search.playlists.items as item (item.id)}
              <li>
                <button type="button" class="mini-card" onclick={() => onOpenPlaylist(item.id)}>
                  <span class="mini-cover">
                    <CoverArt url={item.cover_url} title={item.title} size={100} />
                  </span>
                  <strong>{item.title}</strong>
                  <span>{item.owner ?? '—'} {item.track_count !== null ? `· ${item.track_count} ${tracksWord(item.track_count)}` : ''}</span>
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if searchHasMore}
        <button
          type="button"
          class="ghost more"
          disabled={searchLoadingMore}
          onclick={loadMoreSearch}
        >
          {searchLoadingMore ? 'Загрузка…' : 'Показать ещё'}
        </button>
      {/if}
    </div>
  {:else}
    {#if tracks.length === 0}
      <div class="empty">
        <Icon name="music" size={28} />
        <div>
          <strong>Здесь пока нет треков</strong>
          <p>Сервер вернул пустой состав — это не ошибка загрузки.</p>
        </div>
      </div>
    {:else}
      <ol class="track-list" aria-label="Треки">
        {#each tracks as track, index (track.id)}
          <YandexTrackRow
            {track}
            {index}
            current={currentTrackId === track.id}
            {busy}
            {onPlay}
            {onEnqueue}
            onRemove={target.kind === 'playlist' ? removePlaylistTrack : null}
          />
        {/each}
      </ol>
    {/if}
    {#if artistHasMore}
      <button
        type="button"
        class="ghost more"
        disabled={artistLoadingMore}
        onclick={loadMoreArtist}
      >
        {artistLoadingMore ? 'Загрузка…' : `Показать ещё (${tracks.length} из ${artistTotal})`}
      </button>
    {/if}
  {/if}
</section>

<style>
  .catalog {
    padding: clamp(18px, 2.2vw, 28px);
    border-radius: 26px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }

  .catalog-head {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .back {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px 4px 4px;
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 600;
  }

  .back:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  .head-main {
    display: flex;
    align-items: flex-end;
    gap: 16px;
    min-width: 0;
  }

  .cover {
    flex: none;
    width: 116px;
    height: 116px;
    border-radius: 16px;
    overflow: hidden;
    display: grid;
    place-items: center;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.04);
    color: var(--muted);
  }

  .head-copy {
    min-width: 0;
  }

  .eyebrow {
    color: var(--accent-2);
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  h1 {
    margin: 2px 0 4px;
    font-size: clamp(1.4rem, 2.8vw, 2.1rem);
    letter-spacing: -0.02em;
    overflow-wrap: anywhere;
  }

  .sub {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
  }

  .live-search {
    margin-top: 4px;
  }

  .live-search input {
    width: min(430px, 100%);
    padding: 9px 16px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .live-search input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .primary,
  .ghost {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 13px;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 700;
  }

  .primary {
    border: 0;
    background: var(--accent);
    color: #0b0715;
  }

  .ghost {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
  }

  .ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .ghost.danger {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
  }

  .ghost.small {
    padding: 5px 10px;
    font-size: 0.68rem;
  }

  .track-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .skeletons {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .skeleton-row {
    height: 56px;
    border-radius: 12px;
    background: linear-gradient(100deg, rgba(255, 255, 255, 0.04) 25%, rgba(255, 255, 255, 0.09) 45%, rgba(255, 255, 255, 0.04) 65%);
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .notice {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 12px 14px;
    border-radius: 14px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.78rem;
  }

  .notice.error {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
  }

  .notice span { flex: 1 1 220px; overflow-wrap: anywhere; }

  .empty {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 22px;
    border-radius: 18px;
    border: 1px dashed var(--border);
    color: var(--muted);
  }

  .empty strong { display: block; color: var(--text); margin-bottom: 3px; }
  .empty p { margin: 0; font-size: 0.76rem; }

  .search-sections {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }

  .block-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 6px;
  }

  .block-head h2 {
    margin: 0;
    font-size: 0.92rem;
  }

  .count {
    color: var(--muted);
    font-size: 0.7rem;
    font-variant-numeric: tabular-nums;
  }

  .muted { color: var(--muted); font-size: 0.78rem; }

  .more {
    align-self: center;
    padding: 8px 16px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.74rem;
    font-weight: 700;
  }

  .card-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 10px;
  }

  .mini-card {
    display: flex;
    flex-direction: column;
    gap: 3px;
    width: 100%;
    padding: 9px;
    border-radius: 14px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.025);
    text-align: left;
  }

  .mini-card:hover {
    background: var(--surface-hover);
  }

  .mini-cover {
    width: 100%;
    aspect-ratio: 1;
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 4px;
    background: rgba(255, 255, 255, 0.04);
  }

  .mini-cover.round {
    border-radius: 999px;
  }

  .mini-card strong {
    color: var(--text);
    font-size: 0.76rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mini-card span {
    color: var(--muted);
    font-size: 0.66rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sr-loading {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }

  @keyframes shimmer {
    0% { background-position: 120% 0; }
    100% { background-position: -40% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-row { animation: none; }
  }

  @media (max-width: 640px) {
    .head-main { align-items: flex-start; }
    .cover { width: 86px; height: 86px; }
    .head-actions .primary,
    .head-actions .ghost { flex: 1 1 auto; justify-content: center; }
  }
</style>
