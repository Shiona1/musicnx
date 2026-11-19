<script lang="ts">
  import { dialogFade } from '../lib/motion';
  import { focusInput, keepFocus, trapTabKey } from '../lib/modal';
  import { onDestroy, onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import { api, ApiError, uploadAudioFile } from '../lib/api';
  import {
    artistNames,
    availabilityLabel,
    coverBackground,
    coverInitials,
    albumsWord,
    formatDuration,
    tracksWord,
  } from '../lib/art';
  import { libraryVersion } from '../lib/stores';
  import type {
    AlbumSummary,
    ArtistSummary,
    AuthStatus,
    ImportJob,
    Track,
  } from '../lib/types';

  export let onPlay: (trackId: number) => void;
  export let onEnqueue: (trackId: number) => void;
  export let onOpenAlbum: ((album: AlbumSummary) => void) | null = null;
  export let onOpenArtist: ((artist: ArtistSummary) => void) | null = null;

  type Tab = 'tracks' | 'albums' | 'artists' | 'local';

  const tabs: { id: Tab; label: string }[] = [
    { id: 'tracks', label: 'Треки' },
    { id: 'albums', label: 'Альбомы' },
    { id: 'artists', label: 'Исполнители' },
    { id: 'local', label: 'Локальные файлы' },
  ];
  const activeStatuses = new Set(['queued', 'running', 'verifying', 'cancelling']);
  // Status polling must not silently die: retry with a growing delay, then give
  // the import button back instead of leaving it disabled forever.
  const POLL_BACKOFF_MS = [1000, 2000, 5000];
  const POLL_MAX_ATTEMPTS = 8;
  const STATUS_UNAVAILABLE =
    'Статус импорта недоступен: сервер не отвечает. Импорт разблокирован — попробуйте ещё раз.';

  let tab: Tab = 'tracks';
  let search = '';
  let availability = 'all';
  let offset = 0;
  const limit = 50;

  let loading = false;
  let error = '';
  let tracks: Track[] = [];
  let albums: AlbumSummary[] = [];
  let artists: ArtistSummary[] = [];
  let total = 0;

  let auth: AuthStatus | null = null;
  let job: ImportJob | null = null;
  let importError = '';
  let showPin = false;
  let pin = '';
  let pinError = '';
  let pinBusy = false;
  let uploadInput: HTMLInputElement | null = null;
  let uploading = false;
  let uploadTotal = 0;
  let uploadDone = 0;
  let uploadPercent = 0;
  let uploadError = '';
  let removingId: number | null = null;
  let editing: Track | null = null;
  let editTitle = '';
  let editArtists = '';
  let editAlbum = '';
  let editYear = '';
  let editGenre = '';
  let editBusy = false;
  let editError = '';

  let disposed = false;
  let pollTimer: number | undefined;
  let pollAttempts = 0;
  let requestSeq = 0;
  let lastVersion = 0;

  $: importing = job !== null && activeStatuses.has(job.status);
  $: pageEnd = offset + limit >= total;
  $: phaseLabel = job ? phaseText(job.phase) : '';

  onMount(() => {
    void loadAuth();
    void load();
    void refreshJob();
    const unsubscribe = libraryVersion.subscribe((value) => {
      if (value !== lastVersion) {
        lastVersion = value;
        if (value > 0) void load();
      }
    });
    return () => {
      disposed = true;
      unsubscribe();
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    };
  });

  function phaseText(phase: string): string {
    switch (phase) {
      case 'discovering':
        return 'Поиск файлов';
      case 'importing':
        return 'Импорт';
      case 'verifying':
        return 'Проверка файлов';
      case 'done':
        return 'Готово';
      case 'failed':
        return 'Ошибка';
      case 'cancelled':
        return 'Отменено';
      default:
        return phase;
    }
  }

  function errorText(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.code === 'pin_required') {
        return 'Импорт с другого устройства требует PIN. Задайте security.pin в конфиге сервера.';
      }
      if (err.code === 'path_not_allowed') return 'Путь вне разрешённых корней библиотеки.';
      return err.message;
    }
    return 'Не удалось выполнить запрос.';
  }

  async function loadAuth(): Promise<void> {
    try {
      auth = await api.authStatus();
    } catch {
      auth = null;
    }
  }

  async function load(): Promise<void> {
    const token = ++requestSeq;
    loading = true;
    error = '';
    try {
      if (tab === 'albums') {
        const page = await api.albums({ limit, offset, search });
        if (token !== requestSeq) return;
        albums = page.items;
        total = page.total;
      } else if (tab === 'artists') {
        const page = await api.artists({ limit, offset, search });
        if (token !== requestSeq) return;
        artists = page.items;
        total = page.total;
      } else {
        const page = await api.libraryTracks({
          limit,
          offset,
          search,
          availability: tab === 'local' ? 'offline' : availability,
        });
        if (token !== requestSeq) return;
        tracks = page.items;
        total = page.total;
      }
    } catch (err) {
      if (token === requestSeq) error = errorText(err);
    } finally {
      if (token === requestSeq) loading = false;
    }
  }

  function setTab(next: Tab): void {
    if (tab === next) return;
    tab = next;
    offset = 0;
    search = '';
    void load();
  }

  const tabButtons: Record<Tab, HTMLButtonElement | undefined> = {
    tracks: undefined,
    albums: undefined,
    artists: undefined,
    local: undefined,
  };

  function handleTablistKeydown(event: KeyboardEvent): void {
    // WAI-ARIA tabs pattern: arrows move between tabs, Home/End jump to ends.
    const ids = tabs.map((item) => item.id);
    const index = ids.indexOf(tab);
    let next: number;
    if (event.key === 'ArrowRight') next = (index + 1) % ids.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + ids.length) % ids.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = ids.length - 1;
    else return;
    event.preventDefault();
    const nextId = ids[next];
    if (nextId !== tab) setTab(nextId);
    queueMicrotask(() => tabButtons[nextId]?.focus());
  }

  function submitSearch(event: SubmitEvent): void {
    event.preventDefault();
    offset = 0;
    void load();
  }

  // Instant search: the local FTS index is cheap, typing re-queries with a
  // short debounce instead of waiting for the submit button.
  let searchDebounce: ReturnType<typeof setTimeout> | null = null;

  onDestroy(() => {
    if (searchDebounce !== null) window.clearTimeout(searchDebounce);
  });

  function onSearchInput(): void {
    if (searchDebounce !== null) window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      searchDebounce = null;
      offset = 0;
      void load();
    }, 350);
  }

  function changeAvailability(event: Event): void {
    availability = (event.currentTarget as HTMLSelectElement).value;
    offset = 0;
    void load();
  }

  function nextPage(): void {
    offset += limit;
    void load();
  }

  function previousPage(): void {
    offset = Math.max(0, offset - limit);
    void load();
  }

  function openAlbum(album: AlbumSummary): void {
    // Detail pages replace the old "substitute into search" behavior.
    if (onOpenAlbum) onOpenAlbum(album);
    else {
      tab = 'tracks';
      search = album.title;
      offset = 0;
      void load();
    }
  }

  function openArtist(artist: ArtistSummary): void {
    if (onOpenArtist) onOpenArtist(artist);
    else {
      tab = 'tracks';
      search = artist.name;
      offset = 0;
      void load();
    }
  }

  function pickUploadFiles(): void {
    if (uploading || importing) return;
    uploadInput?.click();
  }

  async function handleUploadFiles(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (files.length === 0) return;
    if (importing) {
      uploadError = 'Дождитесь завершения текущего импорта, затем загрузите файлы.';
      return;
    }

    uploading = true;
    uploadError = '';
    uploadTotal = files.length;
    uploadDone = 0;
    uploadPercent = 0;
    const paths: string[] = [];
    try {
      for (const file of files) {
        const saved = await uploadAudioFile(file, (sent, total) => {
          const filePart = total > 0 ? sent / total : 0;
          uploadPercent = Math.round(((uploadDone + filePart) / uploadTotal) * 100);
        });
        paths.push(saved.path);
        uploadDone += 1;
        uploadPercent = Math.round((uploadDone / uploadTotal) * 100);
      }
      if (paths.length === 0) return;
      job = await api.startImport(paths);
      pollAttempts = 0;
      schedulePoll(500);
      importError = '';
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        uploadError =
          'Файлы загружены, но импорт уже идёт. Дождитесь его и нажмите «Импортировать» — папка uploads будет просканирована.';
      } else {
        uploadError = errorText(err);
      }
    } finally {
      uploading = false;
    }
  }

  async function startImport(): Promise<void> {
    importError = '';
    try {
      job = await api.startImport();
      pollAttempts = 0;
      schedulePoll(500);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        showPin = true;
        pinError = '';
        return;
      }
      if (err instanceof ApiError && (err.status === 409 || err.code === 'import_running')) {
        // An import is already running (another tab or a retried request):
        // attach to it instead of showing a dead-end error.
        pollAttempts = 0;
        await refreshJob();
        return;
      }
      importError = errorText(err);
    }
  }

  async function submitPin(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!pin || pinBusy) return;
    pinBusy = true;
    pinError = '';
    try {
      await api.login(pin);
      showPin = false;
      pin = '';
      await loadAuth();
      await startImport();
    } catch (err) {
      pinError = errorText(err);
    } finally {
      pinBusy = false;
    }
  }

  function startEdit(track: Track): void {
    editing = track;
    editTitle = track.title;
    editArtists = track.artists.map((artist) => artist.name).join(', ');
    editAlbum = track.album?.title ?? '';
    editYear = track.album?.year ? String(track.album.year) : '';
    editGenre = track.genre ?? '';
    editError = '';
  }

  function closeEdit(): void {
    editing = null;
    editError = '';
  }

  function handleModalKeydown(event: KeyboardEvent): void {
    // Both LibraryView dialogs (tag editor, PIN) close on Escape; App-level
    // hotkeys are suppressed by the role="dialog" guard while either is open.
    if (event.key !== 'Escape') return;
    if (editing) closeEdit();
    else if (showPin) showPin = false;
  }

  async function saveEdit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!editing || editBusy) return;
    const artists = editArtists
      .split(',')
      .map((name) => name.trim())
      .filter(Boolean);
    if (!editTitle.trim() || artists.length === 0) {
      editError = 'Название и хотя бы один исполнитель обязательны.';
      return;
    }
    editBusy = true;
    editError = '';
    try {
      const year = Number.parseInt(editYear, 10);
      const changes: Record<string, string | number | string[]> = {
        title: editTitle.trim(),
        artists,
      };
      if (editAlbum.trim()) changes.album = editAlbum.trim();
      if (Number.isFinite(year)) changes.year = year;
      if (editGenre.trim()) changes.genre = editGenre.trim();
      const result = await api.updateTrack(editing.id, changes);
      editing = result.track;
      await load();
      closeEdit();
    } catch (err) {
      editError = errorText(err);
    } finally {
      editBusy = false;
    }
  }

  async function removeTrack(track: Track): Promise<void> {
    if (removingId !== null) return;
    if (
      typeof window !== 'undefined' &&
      !window.confirm(
        `Убрать «${track.title}» из библиотеки? Файл на диске останется; очередь и плейлисты обновятся.`,
      )
    ) {
      return;
    }
    removingId = track.id;
    importError = '';
    try {
      await api.deleteTrack(track.id, false);
      await load();
    } catch (err) {
      importError = errorText(err);
    } finally {
      removingId = null;
    }
  }


  async function cancelImport(): Promise<void> {
    try {
      job = await api.cancelImport();
      pollAttempts = 0;
      schedulePoll(500);
    } catch (err) {
      importError = errorText(err);
    }
  }

  async function refreshJob(): Promise<void> {
    if (disposed) return;
    try {
      const payload = await api.importStatus();
      if (disposed) return;
      const previous = job;
      pollAttempts = 0;
      if (importError === STATUS_UNAVAILABLE) importError = '';
      job = payload.job;
      if (job && activeStatuses.has(job.status)) {
        schedulePoll(700);
        return;
      }
      // The tracked import finished (or was already finished when we attached):
      // refresh the data it may have changed. The initial mount already loads
      // both, so an idle status with no job stays a no-op.
      if (job !== null || previous !== null) {
        await load();
        await loadAuth();
      }
    } catch {
      if (disposed) return;
      pollAttempts += 1;
      if (pollAttempts < POLL_MAX_ATTEMPTS) {
        schedulePoll(POLL_BACKOFF_MS[Math.min(pollAttempts - 1, POLL_BACKOFF_MS.length - 1)]);
        return;
      }
      // The server stayed unreachable: drop the stale job so the import button
      // is usable again, and explain what happened.
      pollAttempts = 0;
      job = null;
      importError = STATUS_UNAVAILABLE;
    }
  }

  function schedulePoll(delay: number): void {
    if (disposed) return;
    if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(() => {
      pollTimer = undefined;
      void refreshJob();
    }, delay);
  }

  function coverStyle(url: string | null, seed: string): string {
    return coverBackground(url, seed);
  }

</script>

<svelte:window onkeydown={handleModalKeydown} />

<section class="library material-content" aria-labelledby="library-title">
  <div class="library-head">
    <div>
      <span class="eyebrow">Локальная медиатека</span>
      <h1 id="library-title">Библиотека</h1>
      <p class="sub">
        Импорт из разрешённых папок, теги, обложки и офлайн-статус.
      </p>
    </div>
    <div class="import-actions">
      <button
        class="primary"
        type="button"
        disabled={importing || uploading}
        onclick={startImport}
      >
        {importing ? 'Импорт…' : 'Импортировать'}
      </button>
      <button
        class="ghost"
        type="button"
        disabled={importing || uploading}
        onclick={pickUploadFiles}
      >
        {uploading ? `Загрузка ${uploadDone}/${uploadTotal}…` : 'Загрузить музыку'}
      </button>
      <input
        bind:this={uploadInput}
        class="file-input"
        type="file"
        accept=".mp3,.flac,.aac,.m4a,.wav,audio/*"
        multiple
        onchange={handleUploadFiles}
      />
      {#if importing}
        <button type="button" class="ghost danger" onclick={cancelImport}>Отмена</button>
      {/if}
    </div>
  </div>

  {#if uploading}
    <div class="import-progress" aria-live="polite">
      <div class="progress-top">
        <span class="phase">Загрузка файлов</span>
        <span class="percent">{uploadPercent}%</span>
      </div>
      <div class="bar"><span style={`width: ${uploadPercent}%`}></span></div>
      <div class="counts">
        <span>файлов: {uploadDone}/{uploadTotal}</span>
      </div>
    </div>
  {/if}

  {#if uploadError}
    <div class="notice error" role="alert">{uploadError}</div>
  {/if}

  {#if importError}
    <div class="notice error" role="alert">{importError}</div>
  {/if}

  {#if job}
    <div class="import-progress" aria-live="polite">
      <div class="progress-top">
        <span class="phase">{phaseLabel}</span>
        <span class="percent">{job.percent}%</span>
      </div>
      <div class="bar"><span style={`width: ${job.percent}%`}></span></div>
      <div class="counts">
        <span>новых: {job.imported}</span>
        <span>обновлено: {job.updated}</span>
        <span>пропущено: {job.skipped}</span>
        {#if job.missing}<span class="warn-text">пропало: {job.missing}</span>{/if}
        <span>файлов: {job.processed}/{job.total}</span>
        {#if job.phase === 'verifying'}<span>проверено: {job.verify_processed}/{job.verify_total}</span>{/if}
      </div>
      {#if job.errors.length}
        <ul class="import-errors">
          {#each job.errors.slice(0, 3) as message}
            <li>{message}</li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}

  <div class="toolbar">
    <div class="tabs" role="tablist" aria-label="Разделы библиотеки">
      {#each tabs as item}
        <button
          type="button"
          role="tab"
          id={`library-tab-${item.id}`}
          aria-selected={tab === item.id}
          aria-controls="library-panel"
          tabindex={tab === item.id ? 0 : -1}
          class:active={tab === item.id}
          bind:this={tabButtons[item.id]}
          onclick={() => setTab(item.id)}
          onkeydown={handleTablistKeydown}
        >
          {item.label}
        </button>
      {/each}
    </div>

    <form class="search" onsubmit={submitSearch}>
      <input
        type="search"
        placeholder={tab === 'albums' ? 'Поиск альбомов' : tab === 'artists' ? 'Поиск исполнителей' : 'Поиск треков'}
        bind:value={search}
        oninput={onSearchInput}
        aria-label="Поиск по библиотеке"
      />
      <button type="submit" aria-label="Искать"><Icon name="search" size={17} /></button>
    </form>

    {#if tab === 'tracks'}
      <label class="filter">
        <span>Доступность</span>
        <select value={availability} onchange={changeAvailability}>
          <option value="all">Все</option>
          <option value="offline">Офлайн</option>
          <option value="online">Онлайн</option>
          <option value="metadata_only">Только метаданные</option>
          <option value="missing">Файл пропал</option>
          <option value="unavailable">Недоступно</option>
        </select>
      </label>
    {/if}
  </div>

  <div id="library-panel" role="tabpanel" tabindex="0" aria-labelledby={`library-tab-${tab}`}>
  {#if loading}
    <div class="state" aria-live="polite">Загрузка…</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else if tab === 'albums'}
    {#if albums.length === 0}
      <div class="state">
        Альбомов нет. Добавьте музыку в разрешённую папку и нажмите «Импортировать».
      </div>
    {:else}
      <ul class="album-grid">
        {#each albums as album (album.id)}
          <li>
            <button type="button" class="album-card" onclick={() => openAlbum(album)}>
              <span
                class="album-cover"
                style={coverStyle(album.cover_url, `${album.title}|${album.artist?.name ?? ''}`)}
                aria-hidden="true"
              >
                {#if !album.cover_url}<span>{coverInitials(album.title)}</span>{/if}
              </span>
              <span class="album-title">{album.title}</span>
              <span class="album-meta">
                {album.artist?.name ?? 'Неизвестный исполнитель'}{album.year ? ` · ${album.year}` : ''}
              </span>
              <span class="album-count">{album.track_count} {tracksWord(album.track_count)}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  {:else if tab === 'artists'}
    {#if artists.length === 0}
      <div class="state">Исполнителей нет.</div>
    {:else}
      <ul class="artist-grid">
        {#each artists as artist (artist.id)}
          <li>
            <button type="button" class="artist-card" onclick={() => openArtist(artist)}>
              <span
                class="artist-avatar"
                style={coverStyle(artist.cover_url, artist.name)}
                aria-hidden="true"
              >
                {#if !artist.cover_url}<span>{coverInitials(artist.name)}</span>{/if}
              </span>
              <span class="artist-name">{artist.name}</span>
              <span class="artist-meta">
                {artist.track_count} {tracksWord(artist.track_count)} · {artist.album_count} {albumsWord(artist.album_count)}
              </span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  {:else}
    {#if tracks.length === 0}
      <div class="state">
        {tab === 'local'
          ? 'Локальных файлов пока нет. Импорт добавляет их после проверки.'
          : 'Ничего не найдено.'}
      </div>
    {:else}
      <ul class="track-list">
        {#each tracks as track (track.id)}
          <li class="track-row">
            <span
              class="row-cover"
              style={coverStyle(track.cover_url, `${track.title}|${track.album?.title ?? ''}`)}
              aria-hidden="true"
            >
              {#if !track.cover_url}<span>{coverInitials(track.title)}</span>{/if}
            </span>
            <span class="row-main">
              <span class="row-title">{track.title}</span>
              <span class="row-artist">{artistNames(track.artists)}</span>
            </span>
            <span class="row-album">{track.album?.title ?? '—'}</span>
            <span class="row-duration">{formatDuration(track.duration_ms)}</span>
            <span class="badge {track.availability}">{availabilityLabel(track.availability)}</span>
            <span class="row-actions">
              <button
                type="button"
                aria-label={`Играть «${track.title}»`}
                title="Играть"
                onclick={() => onPlay(track.id)}
              >
                <Icon name="play" size={16} />
              </button>
              <button
                type="button"
                aria-label={`Добавить «${track.title}» в очередь`}
                title="В очередь"
                onclick={() => onEnqueue(track.id)}
              >
                <Icon name="plus" size={16} />
              </button>
              <button
                type="button"
                aria-label={`Изменить теги «${track.title}»`}
                title="Изменить теги"
                onclick={() => startEdit(track)}
              >
                <Icon name="dots" size={16} />
              </button>
              <button
                type="button"
                aria-label={`Убрать «${track.title}» из библиотеки`}
                title="Убрать из библиотеки"
                disabled={removingId !== null}
                onclick={() => void removeTrack(track)}
              >
                <Icon name="close" size={15} />
              </button>
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  <div class="pager">
    <span class="total">Всего: {total}</span>
    <div class="pager-buttons">
      <button type="button" disabled={offset === 0 || loading} onclick={previousPage}>Назад</button>
      <button type="button" disabled={pageEnd || loading} onclick={nextPage}>Вперёд</button>
    </div>
  </div>
  </div>
</section>

{#if editing}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    use:keepFocus
    onclick={(event) => event.target === event.currentTarget && closeEdit()}
  >
    <div
      class="modal material-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="edit-title"
      tabindex="-1"
      onkeydown={trapTabKey}
    >
      <form onsubmit={saveEdit}>
        <h2 id="edit-title">Теги трека</h2>
        <label>
          Название
          <input bind:value={editTitle} maxlength="300" required use:focusInput />
        </label>
        <label>
          Исполнители (через запятую)
          <input bind:value={editArtists} maxlength="500" required />
        </label>
        <div class="edit-grid">
          <label>
            Альбом
            <input bind:value={editAlbum} maxlength="300" />
          </label>
          <label>
            Год
            <input bind:value={editYear} type="number" min="0" max="9999" />
          </label>
        </div>
        <label>
          Жанр
          <input bind:value={editGenre} maxlength="200" />
        </label>
        {#if editError}<p class="edit-error" role="alert">{editError}</p>{/if}
        <div class="edit-actions">
          <button type="button" class="ghost" onclick={closeEdit}>Отмена</button>
          <button type="submit" class="primary" disabled={editBusy}>
            {editBusy ? 'Сохранение…' : 'Сохранить'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

{#if showPin}
  <div
    class="modal-backdrop material-scrim" transition:dialogFade
    role="presentation"
    use:keepFocus
    onclick={(event) => event.target === event.currentTarget && (showPin = false)}
  >
    <div
      class="modal material-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pin-title"
      tabindex="-1"
      onkeydown={trapTabKey}
    >
      <form onsubmit={submitPin}>
        <h2 id="pin-title">PIN для изменения библиотеки</h2>
        <p>Импорт и настройки защищены PIN-кодом. Просмотр и плеер доступны без него.</p>
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
          <button type="button" class="ghost" onclick={() => (showPin = false)}>Отмена</button>
          <button type="submit" class="primary" disabled={pinBusy || !pin}>Войти</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .library {
    border: 1px solid var(--border);
    background: rgba(10, 14, 24, 0.58);
    border-radius: 26px;
    padding: clamp(18px, 2.2vw, 28px);
  }

  .library-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 18px;
    flex-wrap: wrap;
    margin-bottom: 14px;
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
    max-width: 60ch;
  }

  .import-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }

  .file-input {
    display: none;
  }

  button.primary,
  button.ghost {
    padding: 9px 16px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
  }

  button.primary {
    border: 0;
    color: #0b0715;
    background: var(--accent);
    box-shadow: 0 12px 26px -14px rgba(139, 92, 246, 0.8);
  }

  button.ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  button.ghost.danger {
    color: var(--danger);
    border-color: rgba(248, 113, 113, 0.35);
  }

  .notice {
    margin: 0 0 12px;
    padding: 10px 14px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--surface);
    font-size: 0.78rem;
    line-height: 1.45;
  }

  .notice.warn {
    color: #fcd34d;
    border-color: rgba(252, 211, 77, 0.3);
    background: rgba(252, 211, 77, 0.07);
  }

  .notice.error {
    color: #fecdd3;
    border-color: rgba(248, 113, 113, 0.3);
    background: rgba(248, 113, 113, 0.08);
  }

  .import-progress {
    margin-bottom: 14px;
    padding: 13px 15px;
    border-radius: 14px;
    border: 1px solid rgba(34, 211, 238, 0.22);
    background: rgba(34, 211, 238, 0.06);
  }

  .progress-top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    font-size: 0.78rem;
    font-weight: 600;
  }

  .phase {
    color: #a5f3fc;
  }

  .percent {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }

  .bar {
    height: 6px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.1);
    overflow: hidden;
    margin: 8px 0 7px;
  }

  .bar span {
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width 240ms ease;
  }

  .counts {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    color: var(--muted);
    font-size: 0.7rem;
  }

  .warn-text {
    color: #fca5a5;
  }

  .import-errors {
    margin: 8px 0 0;
    padding-left: 18px;
    color: #fecdd3;
    font-size: 0.72rem;
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 8px 0 14px;
    border-bottom: 1px solid var(--border);
  }

  .tabs {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }

  .tabs button {
    padding: 7px 12px;
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.76rem;
    font-weight: 600;
    border: 1px solid transparent;
  }

  .tabs button:hover {
    color: var(--text);
    background: var(--surface);
  }

  .tabs button.active {
    color: var(--text);
    background: var(--surface);
    border-color: var(--border);
  }

  .search {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: auto;
    padding: 3px 4px 3px 11px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
  }

  .search input {
    border: 0;
    background: transparent;
    color: var(--text);
    font-size: 0.78rem;
    width: clamp(130px, 20vw, 220px);
  }

  .search input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
    border-radius: 999px;
  }

  .search input::placeholder {
    color: var(--muted);
  }

  .search button {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    color: var(--muted);
  }

  .search button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  .filter {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: 0.72rem;
  }

  .filter select {
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 6px 8px;
    font-size: 0.74rem;
  }

  .state {
    padding: 34px 16px;
    text-align: center;
    color: var(--muted);
    font-size: 0.84rem;
  }

  .state.error {
    color: #fecdd3;
  }

  .track-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .track-row {
    display: grid;
    grid-template-columns: 44px minmax(0, 1.6fr) minmax(0, 1fr) auto auto auto;
    align-items: center;
    gap: 12px;
    padding: 9px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.045);
    border-radius: 10px;
  }

  .track-row:hover {
    background: rgba(255, 255, 255, 0.035);
  }

  .row-cover {
    width: 42px;
    height: 42px;
    border-radius: 10px;
    background-size: cover;
    background-position: center;
    display: grid;
    place-items: center;
    font-size: 0.72rem;
    font-weight: 800;
    color: rgba(255, 255, 255, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.1);
    overflow: hidden;
  }

  .row-main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 1px;
  }

  .row-title {
    font-size: 0.84rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-artist,
  .row-album {
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-duration {
    color: var(--muted);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }

  .badge {
    padding: 3px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    font-size: 0.64rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .badge.offline {
    color: #86efac;
  }
  .badge.online {
    color: #67e8f9;
  }
  .badge.missing,
  .badge.unavailable {
    color: #fca5a5;
  }
  .badge.metadata_only {
    color: #c4b5fd;
  }

  .row-actions {
    display: flex;
    gap: 4px;
  }

  .row-actions button {
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 9px;
    color: var(--muted);
    border: 1px solid transparent;
  }

  .row-actions button:hover {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--border);
  }

  /* Touch devices: row controls meet the 44x44px target floor. */
  @media (pointer: coarse) {
    .row-actions button {
      width: 44px;
      height: 44px;
    }
  }

  .album-grid,
  .artist-grid {
    list-style: none;
    margin: 16px 0 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 14px;
  }

  .album-card,
  .artist-card {
    width: 100%;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px;
    border-radius: 16px;
    border: 1px solid transparent;
    color: var(--text);
  }

  .album-card:hover,
  .artist-card:hover {
    background: var(--surface);
    border-color: var(--border);
  }

  .album-cover {
    width: 100%;
    aspect-ratio: 1;
    border-radius: 12px;
    background-size: cover;
    background-position: center;
    display: grid;
    place-items: center;
    font-size: 1.5rem;
    font-weight: 800;
    color: rgba(255, 255, 255, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.1);
    margin-bottom: 3px;
    overflow: hidden;
  }

  .album-title,
  .artist-name {
    font-size: 0.84rem;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .album-meta,
  .artist-meta,
  .album-count {
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .album-count {
    color: #a5b4fc;
    font-size: 0.68rem;
  }

  .artist-grid {
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  }

  .artist-card {
    flex-direction: row;
    align-items: center;
    gap: 12px;
  }

  .artist-avatar {
    width: 56px;
    height: 56px;
    border-radius: 50%;
    flex: none;
    background-size: cover;
    background-position: center;
    display: grid;
    place-items: center;
    font-weight: 800;
    color: rgba(255, 255, 255, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.12);
    overflow: hidden;
  }

  .artist-card .artist-name,
  .artist-card .artist-meta {
    display: block;
  }

  .artist-card {
    flex-wrap: wrap;
  }

  .artist-card .artist-name {
    flex: 1 1 100px;
  }

  .artist-card .artist-meta {
    flex-basis: 100%;
    padding-left: 68px;
    margin-top: -26px;
  }

  .pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-top: 14px;
    color: var(--muted);
    font-size: 0.72rem;
  }

  .pager-buttons {
    display: flex;
    gap: 6px;
  }

  .pager-buttons button {
    padding: 6px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.72rem;
  }

  .pager-buttons button:hover:not(:disabled) {
    color: var(--text);
    background: var(--surface-hover);
  }

  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: grid;
    place-items: center;
    background: rgba(3, 5, 10, 0.72);
    padding: 20px;
  }

  .modal {
    width: min(380px, 100%);
    padding: 22px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: #0d1220;
    box-shadow: 0 30px 70px -20px rgba(0, 0, 0, 0.9);
  }

  .modal h2 {
    margin: 0 0 6px;
    font-size: 1.05rem;
  }

  .modal p {
    margin: 0 0 14px;
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.45;
  }

  .modal input {
    width: 100%;
    padding: 10px 12px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.9rem;
  }

  .modal input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 1px;
  }

  .modal label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-bottom: 10px;
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
  }

  .modal label input {
    color: var(--text);
    font-weight: 400;
  }

  .edit-grid {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 8px;
  }

  .edit-error {
    margin: 0 0 8px;
    color: #fca5a5;
    font-size: 0.74rem;
  }

  .edit-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 6px;
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
    margin-top: 16px;
  }

  @media (max-width: 760px) {
    .track-row {
      grid-template-columns: 40px minmax(0, 1fr) auto auto;
    }
    .row-album,
    .badge {
      display: none;
    }
    .search {
      margin-left: 0;
      flex: 1 1 100%;
    }
    .search input {
      width: 100%;
    }
    .artist-card .artist-meta {
      padding-left: 0;
      margin-top: 0;
    }
  }
</style>
