<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import YandexShelf from './YandexShelf.svelte';
  import CoverArt from './CoverArt.svelte';
  import WaveCard from './WaveCard.svelte';
  import Icon from './Icon.svelte';
  import { tracksWord } from '../lib/art';
  import { yandexApi } from '../lib/yandex';
  import type { YandexAuthStatus, YandexPlaylist } from '../lib/yandex';
  import {
    refreshYandexAuth,
    refreshYandexCatalogs,
    yandexAuth,
    yandexAuthLoading,
    yandexAuthenticated,
    yandexCatalogError,
    yandexCatalogLoading,
    yandexLikes,
    yandexLikesTotal,
    yandexPlaylists,
    yandexPlaylistsTotal,
  } from '../lib/yandexStores';

  /**
   * P3 Home: likes, playlists and «Моя волна». Recent is an honest placeholder
   * (no history route until a later phase) and Downloads is explicitly P4 —
   * no fake ready-made sections (AGENTS.md §12).
   */
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: (trackId: number) => void;
  export let currentTrackId: number | null = null;
  export let position = 0;
  export let busy = false;
  export let onAuthChange: (auth: YandexAuthStatus | null) => void = () => {};
  /** Navigation hooks; existing only when the integrator wires them. */
  export let onOpenSetup: (() => void) | null = null;
  export let onOpenPlaylist: ((playlist: YandexPlaylist) => void) | null = null;
  export let onOpenDownloads: (() => void) | null = null;
  export let onSearch: ((query: string) => void) | null = null;
  export let onToast: ((message: string) => void) | null = null;

  const PREVIEW_LIMIT = 24;

  let accountLabel = '';
  let searchQuery = '';
  let creatingPlaylist = false;

  $: accountLabel = $yandexAuth?.login
    ? `Аккаунт: ${$yandexAuth.login}`
    : 'Аккаунт подключён';
  $: likesLoading = $yandexCatalogLoading && $yandexLikes.length === 0;
  $: playlistsLoading = $yandexCatalogLoading && $yandexPlaylists.length === 0;
  $: likesEmptyTitle = $yandexAuthenticated ? 'Лайков пока нет' : 'Аккаунт не подключён';
  $: likesEmptyHint = $yandexAuthenticated
    ? 'Поставьте лайк треку в Яндекс Музыке — он появится на этой полке.'
    : 'Подключите аккаунт: лайки придут из Яндекс Музыки.';

  onMount(() => {
    void bootstrap();
    const unsubscribe = yandexAuth.subscribe((value) => onAuthChange(value));
    return () => unsubscribe();
  });

  async function bootstrap(): Promise<void> {
    // Setup may already have loaded the session; only ask when it is unknown.
    const status = get(yandexAuth) ?? (await refreshYandexAuth());
    if (status?.authenticated) await refreshYandexCatalogs(PREVIEW_LIMIT);
  }

  function retryCatalogs(): void {
    void refreshYandexCatalogs(PREVIEW_LIMIT);
  }

  function openPlaylist(playlist: YandexPlaylist): void {
    if (onOpenPlaylist) onOpenPlaylist(playlist);
  }

  function submitSearch(event: SubmitEvent): void {
    event.preventDefault();
    const query = searchQuery.trim();
    if (query && onSearch) onSearch(query);
  }

  async function createPlaylist(): Promise<void> {
    if (creatingPlaylist || !$yandexAuthenticated) return;
    const title = window.prompt('Название нового плейлиста в Яндекс Музыке');
    if (!title || !title.trim()) return;
    creatingPlaylist = true;
    try {
      const playlist = await yandexApi.createPlaylist(title.trim(), 'private');
      await refreshYandexCatalogs(PREVIEW_LIMIT);
      onToast?.(`Плейлист «${playlist.title}» создан.`);
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось создать плейлист.');
    } finally {
      creatingPlaylist = false;
    }
  }
</script>

<div class="home" aria-labelledby="home-title">
  <div class="home-head">
    <div class="head-text">
      <span class="eyebrow">Яндекс Музыка</span>
      <h1 id="home-title">Главная</h1>
      <p class="sub">
        Лайки, плейлисты и «Моя волна» из вашего аккаунта. Токен хранится только
        на сервере MusicNX — в браузер он не попадает.
      </p>
    </div>
    <div class="head-actions">
      {#if onSearch}
        <form class="search" onsubmit={submitSearch} role="search">
          <Icon name="search" size={15} />
          <input
            type="search"
            placeholder="Поиск в Яндекс Музыке"
            aria-label="Поиск в Яндекс Музыке"
            bind:value={searchQuery}
            maxlength="200"
          />
          <button type="submit" class="ghost small" disabled={!searchQuery.trim()}>
            Найти
          </button>
        </form>
      {/if}
      {#if $yandexAuthenticated}
        <button
          type="button"
          class="ghost small"
          disabled={creatingPlaylist}
          onclick={() => void createPlaylist()}
        >
          {creatingPlaylist ? 'Создание…' : 'Новый плейлист'}
        </button>
      {/if}
      {#if $yandexAuthenticated}
        <span class="chip account" title="Аккаунт Яндекс Музыки">{accountLabel}</span>
      {/if}
      {#if onOpenSetup}
        <button type="button" class="ghost" onclick={() => onOpenSetup?.()}>
          {$yandexAuthenticated ? 'Подключение' : 'Подключить Яндекс'}
        </button>
      {/if}
    </div>
  </div>

  {#if !$yandexAuthLoading && !$yandexAuthenticated}
    <div class="notice warn">
      <strong>Яндекс Музыка не подключена.</strong>
      <span>
        {#if onOpenSetup}
          Подключите аккаунт по QR-коду или токену, чтобы увидеть лайки,
          плейлисты и волну.
        {:else}
          Раздел подключения (Setup) монтируется интегратором; без него лайки и
          плейлисты недоступны.
        {/if}
      </span>
      {#if onOpenSetup}
        <button type="button" class="link-button" onclick={() => onOpenSetup?.()}>
          Открыть подключение
        </button>
      {/if}
    </div>
  {/if}

  {#if $yandexCatalogError}
    <div class="notice error" role="alert">
      <span>{$yandexCatalogError}</span>
      <button
        type="button"
        class="ghost small"
        disabled={$yandexCatalogLoading}
        onclick={retryCatalogs}
      >
        Повторить
      </button>
    </div>
  {/if}

  <YandexShelf
    title="Мне нравится"
    subtitle="Треки из «Мне нравится» вашего аккаунта"
    tracks={$yandexLikes}
    total={$yandexLikesTotal}
    loading={likesLoading}
    error=""
    emptyTitle={likesEmptyTitle}
    emptyHint={likesEmptyHint}
    {currentTrackId}
    {busy}
    {onPlay}
    {onEnqueue}
  />

  <section class="panel material-content" aria-labelledby="playlists-title">
    <div class="panel-head">
      <div>
        <h2 id="playlists-title">Плейлисты</h2>
        <p class="panel-sub">Свои и чужие плейлисты из аккаунта</p>
      </div>
      {#if !playlistsLoading && $yandexPlaylists.length > 0}
        <span class="count">
          {$yandexPlaylistsTotal > $yandexPlaylists.length
            ? `${$yandexPlaylists.length} из ${$yandexPlaylistsTotal}`
            : `${$yandexPlaylists.length}`}
        </span>
      {/if}
    </div>

    {#if playlistsLoading}
      <div class="loading" role="status">
        <span class="spinner" aria-hidden="true"></span>
        Загружаем плейлисты…
      </div>
    {:else if $yandexPlaylists.length === 0}
      <div class="empty">
        <Icon name="queue" size={28} />
        <div>
          <strong>Плейлистов нет</strong>
          <p>
            {$yandexAuthenticated
              ? 'В аккаунте не нашлось плейлистов, либо у них нет доступных треков.'
              : 'Подключите аккаунт, чтобы увидеть свои плейлисты.'}
          </p>
        </div>
      </div>
    {:else}
      <ul class="playlist-grid">
        {#each $yandexPlaylists as playlist (playlist.id)}
          <li class="playlist">
            {#if onOpenPlaylist}
              <button
                class="playlist-card"
                type="button"
                aria-label={`Открыть плейлист «${playlist.title}»`}
                onclick={() => openPlaylist(playlist)}
              >
                <span class="playlist-cover">
                  <CoverArt url={playlist.cover_url} title={playlist.title} size={300} />
                </span>
                <span class="playlist-title" title={playlist.title}>{playlist.title}</span>
                <span class="playlist-meta">
                  {playlist.track_count !== null ? `${playlist.track_count} ${tracksWord(playlist.track_count)}` : 'треки уточняются'}
                  {#if playlist.owner}· {playlist.owner}{/if}
                </span>
              </button>
            {:else}
              <div class="playlist-card static">
                <span class="playlist-cover">
                  <CoverArt url={playlist.cover_url} title={playlist.title} size={300} />
                </span>
                <span class="playlist-title" title={playlist.title}>{playlist.title}</span>
                <span class="playlist-meta">
                  {playlist.track_count !== null ? `${playlist.track_count} ${tracksWord(playlist.track_count)}` : 'треки уточняются'}
                  {#if playlist.owner}· {playlist.owner}{/if}
                </span>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <WaveCard {onPlay} {onEnqueue} {currentTrackId} {position} {busy} />

  <div class="secondary">
    <section class="panel placeholder" aria-labelledby="downloads-title">
      <div class="panel-head">
        <div>
          <h2 id="downloads-title">Загрузки</h2>
          <p class="panel-sub">Офлайн-доступ к трекам</p>
        </div>
      </div>
      <p class="placeholder-text">
        Очередь загрузок, прогресс и офлайн-полка доступны в разделе «Загрузки».
      </p>
      <p class="placeholder-text">
        <strong>Как скачать:</strong> нажмите кнопку со стрелкой вниз у трека
        (⤓) или «Скачать» в карточке «Сейчас играет». Готовый и проверенный
        файл появится в «Офлайн-полке».
      </p>
      {#if onOpenDownloads}
        <button type="button" class="ghost" onclick={() => onOpenDownloads?.()}>
          Открыть загрузки
        </button>
      {/if}
    </section>
  </div>
</div>

<style>
  .home {
    display: flex;
    flex-direction: column;
    gap: 18px;
    min-width: 0;
  }

  .home-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 18px;
    flex-wrap: wrap;
    padding: 2px 2px 0;
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
    max-width: 62ch;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .search {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 4px 3px 10px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface);
    color: var(--muted);
  }

  .search input {
    width: min(240px, 34vw);
    padding: 5px 2px;
    border: 0;
    background: transparent;
    color: var(--text);
    font-size: 0.76rem;
  }

  .search:focus-within {
    border-color: var(--accent-2);
  }

  .ghost.small {
    padding: 5px 10px;
    font-size: 0.68rem;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip.account {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.32);
    background: rgba(139, 92, 246, 0.12);
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

  .notice {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px 15px;
    border-radius: 16px;
    font-size: 0.8rem;
    border: 1px solid var(--border);
    background: var(--surface);
  }

  .notice.warn {
    border-color: rgba(252, 211, 77, 0.28);
    background: rgba(252, 211, 77, 0.08);
    color: #fde68a;
  }

  .notice.error {
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
  }

  .notice.error span {
    overflow-wrap: anywhere;
  }

  .link-button {
    padding: 0;
    color: #fde68a;
    font-size: inherit;
    font-weight: 700;
    text-decoration: underline;
  }

  .panel {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.55);
    border-radius: 22px;
    padding: clamp(16px, 2vw, 22px);
  }

  .panel-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 14px;
    margin-bottom: 12px;
  }

  h2 {
    margin: 0;
    font-size: 1rem;
    letter-spacing: 0.01em;
  }

  .panel-sub {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 0.74rem;
  }

  .count {
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .loading {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 16px 4px;
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
    max-width: 60ch;
  }

  .playlist-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
    gap: 12px;
  }

  .playlist {
    min-width: 0;
  }

  .playlist-card {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
    padding: 8px;
    border-radius: 16px;
    border: 1px solid transparent;
    background: none;
    color: var(--text);
    text-align: left;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      transform 140ms ease;
  }

  .playlist-card:hover,
  .playlist-card:focus-visible {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--border);
    transform: translateY(-2px);
  }

  .playlist-card.static:hover {
    transform: none;
  }

  .playlist-cover {
    position: relative;
    display: grid;
    place-items: center;
    width: 100%;
    aspect-ratio: 1;
    border-radius: 12px;
    background-size: cover;
    background-position: center;
    border: 1px solid rgba(255, 255, 255, 0.12);
    overflow: hidden;
    color: rgba(255, 255, 255, 0.82);
  }

  .playlist-cover::after {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at 30% 20%, rgba(255, 255, 255, 0.14), transparent 60%);
  }

  .playlist-title {
    font-size: 0.83rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .playlist-meta {
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .secondary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 14px;
  }

  .placeholder {
    border-style: dashed;
    background: rgba(10, 14, 24, 0.4);
  }

  .placeholder-text {
    margin: 0;
    color: var(--muted);
    font-size: 0.78rem;
    max-width: 62ch;
  }

  @media (max-width: 560px) {
    .home-head {
      align-items: flex-start;
    }
    .playlist-grid {
      grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    }
    .chip.account {
      max-width: 100%;
    }
  }
</style>
