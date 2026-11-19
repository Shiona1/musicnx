<script lang="ts">
  import { onMount } from 'svelte';
  import BrowserAudio from './components/BrowserAudio.svelte';
  import CatalogView from './components/CatalogView.svelte';
  import HistoryShelf from './components/HistoryShelf.svelte';
  import AppShell from './components/ui/AppShell.svelte';
  import SystemBar from './components/ui/SystemBar.svelte';
  import PageShell from './components/ui/PageShell.svelte';
  import NowDock from './components/ui/NowDock.svelte';
  import BottomNav from './components/ui/BottomNav.svelte';
  import LibraryDetailView from './components/LibraryDetailView.svelte';
  import QueueDrawer from './components/ui/QueueDrawer.svelte';
  import DownloadsView from './components/DownloadsView.svelte';
  import Home from './components/Home.svelte';
  import KaraokeView from './components/KaraokeView.svelte';
  import LibraryView from './components/LibraryView.svelte';
  import LyricsPanel from './components/LyricsPanel.svelte';
  import NowHero from './components/NowHero.svelte';
  import NetworkPanel from './components/NetworkPanel.svelte';
  import OfflineShelf from './components/OfflineShelf.svelte';
  import Setup from './components/Setup.svelte';
  import SettingsPanel from './components/SettingsPanel.svelte';
  import Shelf from './components/Shelf.svelte';
  import SyncCenter from './components/SyncCenter.svelte';
  import SystemStatusPanel from './components/SystemStatusPanel.svelte';
  import ZonesPanel from './components/ZonesPanel.svelte';
  import Icon from './components/Icon.svelte';
  import { api, ApiError } from './lib/api';
  import { coverUrlAt, tracksWord } from './lib/art';
  import { getCoverAccent, setCoverAccent } from './lib/coverAccent';
  import { MusicSocket } from './lib/ws';
  import { setScrollLocked } from './lib/scrollLock';
  import type { YandexAuthStatus } from './lib/yandex';
  import { yandexAuth as yandexAuthStore, yandexWave } from './lib/yandexStores';
  import { applyDownloadEvent } from './lib/downloadStores';
  import { applyLyricsEvent } from './lib/lyricsStores';
  import { applySyncEvent } from './lib/syncStores';
  import { applyZonesEvent } from './lib/zonesStores';
  import { activeZone, refreshZones } from './lib/zonesStores';
  import {
    applyPlayer,
    applyPosition,
    connection,
    libraryVersion,
    player,
    positionClock,
    queue,
    serverMode,
    serverVersion,
    shelf,
    shelfTotal,
    showToast,
    toastMessage,
    toastTone,
  } from './lib/stores';
  import type { PlayerState, QueueState, WsEvent } from './lib/types';
  import {
    isApPortalPath,
    pathForView,
    parseCatalogPath,
    catalogPath,
    parseLibraryPath,
    libraryPath,
    sectionForView,
    systemPageForView,
    viewFromPath,
  } from './lib/routes';
  import type {
    CatalogTarget,
    LibraryTarget,
    PrimarySection,
    SystemPage,
    View,
  } from './lib/routes';

  let busy = false;
  let displayPosition = 0;
  let frameHandle = 0;
  let socket: MusicSocket | null = null;
  const initialPath = typeof window !== 'undefined' ? window.location.pathname : '/';
  let view: View = viewFromPath(initialPath);
  let catalogTarget: CatalogTarget | null =
    typeof window !== 'undefined'
      ? parseCatalogPath(window.location.pathname, window.location.search)
      : null;
  let libraryTarget: LibraryTarget | null =
    typeof window !== 'undefined' ? parseLibraryPath(window.location.pathname) : null;
  let isApPortal = isApPortalPath(initialPath);
  let systemBrowserZone = false;
  let booted = false;
  let browserZone = false;
  let karaokeOpen = false;
  let queueOpen = false;
  let lastAccentUrl: string | null | undefined = undefined;
  let accentSeq = 0;

  $: currentTrackId = $player?.track?.id ?? null;
  $: ambientUrl = $player?.track ? coverUrlAt($player.track.cover_url, 64) : null;
  $: ambientStyle = ambientUrl ? `background-image: url('${ambientUrl}');` : '';
  $: section = sectionForView(view);
  $: systemPage = systemPageForView(view);
  // The browser zone is a server-side routing decision: once the zone list has
  // arrived it is the source of truth; before that the status flag is.
  $: browserZone = $activeZone ? $activeZone.kind === 'browser' : systemBrowserZone;

  // Cover light is a read-only visual enhancement; fallback tokens remain active
  // until a same-origin cover has been sampled and clamped.
  $: {
    const nextUrl = $player?.track?.cover_url ?? null;
    if (nextUrl !== lastAccentUrl) {
      lastAccentUrl = nextUrl;
      const seq = ++accentSeq;
      void getCoverAccent(nextUrl).then((accent) => {
        if (seq === accentSeq) setCoverAccent(accent);
      });
    }
  }

  function navigate(next: View, replace = false): void {
    const path = pathForView(next);
    if (typeof window !== 'undefined' && window.location.pathname !== path) {
      if (replace) window.history.replaceState({ view: next }, '', path);
      else window.history.pushState({ view: next }, '', path);
    }
    view = next;
    if (next !== 'catalog') catalogTarget = null;
    if (next !== 'libraryAlbum' && next !== 'libraryArtist') libraryTarget = null;
    isApPortal = typeof window !== 'undefined' && isApPortalPath(window.location.pathname);
    if (typeof window !== 'undefined') window.scrollTo(0, 0);
  }

  function openCatalog(target: CatalogTarget): void {
    const path = catalogPath(target);
    if (typeof window !== 'undefined') {
      window.history.pushState({ catalog: target }, '', path);
      window.scrollTo(0, 0);
    }
    catalogTarget = target;
    view = 'catalog';
    isApPortal = false;
  }

  function backFromCatalog(): void {
    if (typeof window !== 'undefined' && window.history.length > 1) {
      window.history.back();
      return;
    }
    navigate('yandex');
  }

  function openLibraryDetail(target: LibraryTarget): void {
    const path = libraryPath(target);
    if (typeof window !== 'undefined') {
      window.history.pushState({ library: target }, '', path);
      window.scrollTo(0, 0);
    }
    libraryTarget = target;
    view = target.kind === 'album' ? 'libraryAlbum' : 'libraryArtist';
    isApPortal = false;
  }

  function backFromLibraryDetail(): void {
    if (typeof window !== 'undefined' && window.history.length > 1) {
      window.history.back();
      return;
    }
    navigate('library');
  }

  function navigateSection(next: PrimarySection): void {
    if (next === 'system') navigate(systemPage);
    else navigate(next);
  }

  function navigateSystem(next: SystemPage): void {
    navigate(next);
  }

  function handlePopState(): void {
    if (typeof window === 'undefined') return;
    view = viewFromPath(window.location.pathname);
    catalogTarget = parseCatalogPath(window.location.pathname, window.location.search);
    libraryTarget = parseLibraryPath(window.location.pathname);
    isApPortal = isApPortalPath(window.location.pathname);
  }

  $: if (typeof document !== 'undefined') {
    // One owner for the scroll lock: overlays must not restore their own
    // `body.style.overflow`, otherwise a close/cleanup race leaves the page
    // permanently unscrollable. The helper also restores the scroll position.
    setScrollLocked(karaokeOpen || queueOpen);
  }

  onMount(() => {
    // Heal sessions that were left with the legacy inline lock by an older
    // build: the new lock is a class on <html>, so a stale body style wins.
    document.body.style.removeProperty('overflow');
    if (!karaokeOpen && !queueOpen) {
      document.documentElement.classList.remove('scroll-locked');
    }
    void loadInitial();
    window.addEventListener('popstate', handlePopState);

    socket = new MusicSocket({
      onStatus: (status) => connection.set(status),
      onSnapshot: (payload) => {
        serverMode.set(payload.server.mode);
        serverVersion.set(payload.server.version);
        applyPlayer(payload.player);
        queue.set(payload.queue);
        shelf.set(payload.library.shelf);
        shelfTotal.set(payload.library.total);
      },
      onEvent: handleEvent,
    });
    socket.start();

    const frame = () => {
      const state = $player;
      const clock = $positionClock;
      if (state?.status === 'playing') {
        const elapsed = performance.now() - clock.at;
        const cap = state.duration_ms || clock.ms + elapsed;
        displayPosition = Math.min(clock.ms + elapsed, cap);
      } else if (state) {
        displayPosition = state.position_ms;
      } else {
        displayPosition = 0;
      }
      frameHandle = requestAnimationFrame(frame);
    };
    frameHandle = requestAnimationFrame(frame);
    window.addEventListener('keydown', handleKeydown);

    return () => {
      document.documentElement.classList.remove('scroll-locked');
      socket?.stop();
      socket = null;
      cancelAnimationFrame(frameHandle);
      window.removeEventListener('keydown', handleKeydown);
      window.removeEventListener('popstate', handlePopState);
    };
  });

  async function loadInitial(): Promise<void> {
    try {
      const [state, queueState, page, system] = await Promise.all([
        api.playerState(),
        api.queue(),
        api.library(24),
        api.systemStatus(),
      ]);
      applyPlayer(state);
      queue.set(queueState);
      shelf.set(page.items);
      shelfTotal.set(page.total);
      systemBrowserZone = Boolean(system.audio?.browser_zone);
      await refreshZones();
    } catch (error) {
      showToast(describeError(error), 'error');
    } finally {
      booted = true;
    }
  }

  async function refreshShelf(): Promise<void> {
    try {
      const page = await api.library(24);
      shelf.set(page.items);
      shelfTotal.set(page.total);
    } catch {
      // A stale shelf is preferable to an error toast for a background refresh.
    }
  }

  function handleEvent(event: WsEvent): void {
    if (applyDownloadEvent(event)) return;
    if (applyLyricsEvent(event)) return;
    if (applySyncEvent(event)) return;
    if (applyZonesEvent(event)) return;
    switch (event.type) {
      case 'player.state_changed':
      case 'player.track_changed': {
        const state = event.payload.state as PlayerState | undefined;
        if (state) applyPlayer(state);
        break;
      }
      case 'player.position': {
        const position = event.payload.position_ms;
        const serverTime = event.payload.server_time_ms;
        if (typeof position === 'number') {
          applyPosition(position, typeof serverTime === 'number' ? serverTime : undefined);
        }
        break;
      }
      case 'queue.updated': {
        const nextQueue = event.payload.queue as QueueState | undefined;
        if (nextQueue) queue.set(nextQueue);
        break;
      }
      case 'volume.changed': {
        const volume = event.payload.volume;
        if (typeof volume === 'number') {
          player.update((state) => (state ? { ...state, volume } : state));
        }
        break;
      }
      case 'system.warning': {
        const code = String(event.payload.code ?? '');
        showToast(warningText(code, event.payload) ?? String(event.payload.message ?? 'Предупреждение сервера'), 'error');
        break;
      }
      case 'library.updated': {
        libraryVersion.update((value) => value + 1);
        void refreshShelf();
        break;
      }
      case 'auth.required': {
        yandexAuthStore.set({
          authenticated: false,
          login: null,
          expires_at: null,
          source: null,
        } satisfies YandexAuthStatus);
        showToast('Яндекс Музыка требует повторного входа в аккаунт.', 'error');
        break;
      }
      case 'auth.status': {
        yandexAuthStore.set(event.payload as unknown as YandexAuthStatus);
        break;
      }
      case 'wave.updated': {
        const wave = event.payload.wave as Parameters<typeof yandexWave.set>[0];
        if (wave) yandexWave.set(wave);
        break;
      }
      default:
        break;
    }
  }

  async function run<T>(operation: Promise<T>, apply?: (value: T) => void): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      const value = await operation;
      apply?.(value);
    } catch (error) {
      showToast(describeError(error), 'error');
    } finally {
      busy = false;
    }
  }

  /**
   * Backend warning codes are stable tokens; the Russian copy lives in the UI
   * so the player can explain skips instead of dumping an English message.
   */
  function warningText(code: string, payload: Record<string, unknown>): string | null {
    switch (code) {
      case 'cdn_unreachable':
        return 'CDN Яндекса недоступен из сети Pi: онлайн-треки пропускаются, офлайн играют.';
      case 'queue_skipped_unavailable': {
        const skipped = Number(payload.skipped ?? 0);
        return skipped > 0
          ? `Пропущено недоступных треков: ${skipped}. Включаю следующий доступный.`
          : 'Пропускаю недоступные треки.';
      }
      case 'queue_exhausted_offline':
        return 'Дальше только недоступные треки — воспроизведение остановлено. Офлайн-треки можно выбрать вручную.';
      case 'track_fallback': {
        const title = String(payload.title ?? '').trim();
        return title
          ? `Исходный трек недоступен — включил другую версию: «${title}».`
          : 'Исходный трек недоступен — включил другую версию.';
      }
      case 'audio_sink_lost':
        return 'Активный аудиовыход пропал — переключаю на локальный. Проверьте Bluetooth/кабель.';
      default:
        return null;
    }
  }

  function describeError(error: unknown): string {
    if (error instanceof ApiError) {
      if (error.code === 'no_playable_source') {
        return 'Нет доступного источника: в real-режиме нужен локальный файл (P2/P3).';
      }
      if (error.code === 'queue_empty') return 'Очередь пуста — выберите трек на полке.';
      if (error.code === 'stream_unavailable') {
        return 'Сервер Pi не смог открыть поток Яндекс Музыки: CDN не отвечает. Проверьте сеть/VPN; офлайн-треки играют.';
      }
      if (error.code === 'cdn_unreachable') {
        return 'CDN Яндекса недоступен из сети Raspberry Pi (провайдер или VPN). Офлайн-треки и Bluetooth работают — выберите трек с меткой «Офлайн».';
      }
      if (error.code === 'unavailable' && /stream|audio backend/i.test(error.message)) {
        return 'Не удалось запустить трек: источник или аудиодвижок недоступен. Подробности — в карточке «Сейчас играет».';
      }
      return error.message;
    }
    return 'Не удалось выполнить действие. Проверьте соединение с сервером.';
  }

  function handlePlayTrack(trackId: number): void {
    void run(api.play(trackId), applyPlayer);
  }

  function handlePlayPause(): void {
    const state = $player;
    if (!state) return;
    if (state.status === 'playing') {
      void run(api.pause(), applyPlayer);
    } else {
      void run(api.play(), applyPlayer);
    }
  }

  function handleNext(): void {
    void run(api.next(), applyPlayer);
  }

  function handlePrevious(): void {
    void run(api.previous(), applyPlayer);
  }

  function handleSeek(positionMs: number): void {
    void run(api.seek(Math.round(positionMs)), applyPlayer);
  }

  function handleVolume(volume: number): void {
    void run(api.setVolume(volume), applyPlayer);
  }

  function handleEnqueue(trackId: number): void {
    void run(api.addToQueue([trackId]), (nextQueue) => {
      queue.set(nextQueue);
      showToast('Трек добавлен в очередь');
    });
  }

  function handleEnqueueMany(trackIds: number[]): void {
    if (trackIds.length === 0) return;
    void run(api.addToQueue(trackIds), (nextQueue) => {
      queue.set(nextQueue);
      showToast(`Добавлено в очередь: ${trackIds.length}`);
    });
  }

  async function handlePlayCollection(trackIds: number[]): Promise<void> {
    if (trackIds.length === 0) return;
    try {
      const nextQueue = await api.addToQueue(trackIds);
      queue.set(nextQueue);
      const state = await api.play(trackIds[0]);
      applyPlayer(state);
      showToast(`Очередь: ${trackIds.length} ${tracksWord(trackIds.length)}, играет первый.`);
    } catch (error) {
      showToast(describeError(error), 'error');
    }
  }

  function handleRemove(itemId: number): void {
    void run(api.removeFromQueue(itemId), (nextQueue) => queue.set(nextQueue));
  }

  function handleToggleShuffle(): void {
    void run(api.setPlayerMode({ shuffle: !($player?.shuffle ?? false) }), applyPlayer);
  }

  function handleCycleRepeat(): void {
    const current = $player?.repeat ?? 'off';
    const next = current === 'off' ? 'all' : current === 'all' ? 'one' : 'off';
    void run(api.setPlayerMode({ repeat: next }), applyPlayer);
  }

  function handleQueueMove(itemId: number, direction: -1 | 1): void {
    const ids = ($queue?.items ?? []).map((item) => item.id);
    const from = ids.indexOf(itemId);
    const to = from + direction;
    if (from < 0 || to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    void run(api.reorderQueue(ids), (nextQueue) => queue.set(nextQueue));
  }

  function handleClearQueue(): void {
    void run(api.clearQueue(), (nextQueue) => queue.set(nextQueue));
  }

  function seekBy(deltaMs: number): void {
    const state = $player;
    if (!state) return;
    const target = Math.max(0, Math.min(displayPosition + deltaMs, state.duration_ms || 0));
    handleSeek(target);
  }

  function handleKeydown(event: KeyboardEvent): void {
    // Holding a key must not turn into a stream of play/pause or seek commands.
    if (event.repeat) return;

    const target = event.target instanceof Element ? event.target : null;
    // Native activation (Space/Enter) belongs to the focused control, and global
    // shortcuts must never fire while a dialog is open — even if focus sits on
    // the dialog background rather than on one of its controls.
    if (
      target?.closest('button, a, [role="button"], [role="tab"], summary') ||
      target?.closest('[role="dialog"]') ||
      document.querySelector('[role="dialog"]') !== null ||
      target?.closest('input, textarea, select, [contenteditable="true"]') ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey
    ) {
      return;
    }
    if (event.code === 'KeyL' && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      if (karaokeOpen) karaokeOpen = false;
      else if ($player?.track) karaokeOpen = true;
      return;
    }
    switch (event.code === 'KeyN' ? 'n' : event.code === 'KeyP' ? 'p' : event.key) {
      case ' ':
        event.preventDefault();
        handlePlayPause();
        break;
      case 'ArrowLeft':
        event.preventDefault();
        seekBy(-5000);
        break;
      case 'ArrowRight':
        event.preventDefault();
        seekBy(5000);
        break;
      case 'n':
      case 'N':
        handleNext();
        break;
      case 'p':
      case 'P':
        handlePrevious();
        break;
      default:
        break;
    }
  }
</script>

<svelte:head>
  <title>{$player?.track ? `${$player.track.title} — MusicNX` : 'MusicNX'}</title>
</svelte:head>

<AppShell ambientStyle={ambientStyle} hasArt={Boolean(ambientUrl)}>
  <SystemBar
    slot="bar"
    section={section}
    systemPage={systemPage}
    mode={$serverMode}
    connection={$connection}
    onnavigate={navigateSection}
    onsystem={navigateSystem}
    onToast={showToast}
  />

  <PageShell slot="main" bare>
    {#if !booted}
      <div class="boot-stack" aria-busy="true" aria-label="Загрузка MusicNX">
        <div class="boot-hero skeleton-block"></div>
        <div class="boot-shelf">
          {#each [0, 1, 2, 3, 4] as key (key)}
            <div class="boot-card skeleton-block"></div>
          {/each}
        </div>
      </div>
    {:else if view === 'library'}
      <LibraryView
        onPlay={handlePlayTrack}
        onEnqueue={handleEnqueue}
        onOpenAlbum={(album) => openLibraryDetail({ kind: 'album', id: album.id })}
        onOpenArtist={(artist) => openLibraryDetail({ kind: 'artist', id: artist.id })}
      />
    {:else if (view === 'libraryAlbum' || view === 'libraryArtist') && libraryTarget}
      <LibraryDetailView
        kind={libraryTarget.kind}
        id={libraryTarget.id}
        onBack={backFromLibraryDetail}
        onPlay={handlePlayTrack}
        onEnqueue={handleEnqueue}
        onPlayAll={(trackIds) => void handlePlayCollection(trackIds)}
        onEnqueueAll={handleEnqueueMany}
        onToast={showToast}
      />
    {:else if view === 'downloads'}
      <DownloadsView onToast={showToast} />
    {:else if view === 'sync'}
      <SyncCenter onToast={showToast} />
    {:else if view === 'state'}
      <SystemStatusPanel onToast={showToast} />
    {:else if view === 'zones'}
      <ZonesPanel mode={$serverMode} onToast={showToast} />
    {:else if view === 'settings'}
      <SettingsPanel onToast={showToast} />
    {:else if view === 'network'}
      {#if isApPortal}
        <div class="ap-portal" role="status">
          <strong>MusicNX · первичная настройка</strong>
          <p>Вы подключены к MusicNX-Setup. Выберите домашнюю Wi-Fi сеть ниже — сервер сохранит её и выключит точку доступа.</p>
        </div>
      {/if}
      <NetworkPanel onToast={showToast} />
    {:else if view === 'setup'}
      <Setup onClose={() => navigate('yandex')} />
    {:else if view === 'catalog' && catalogTarget}
      <CatalogView
        target={catalogTarget}
        {currentTrackId}
        onBack={backFromCatalog}
        onPlay={handlePlayTrack}
        onEnqueue={handleEnqueue}
        onEnqueueMany={handleEnqueueMany}
        onPlayCollection={handlePlayCollection}
        onOpenPlaylist={(id) => openCatalog({ kind: 'playlist', id, query: null })}
        onOpenAlbum={(id) => openCatalog({ kind: 'album', id, query: null })}
        onOpenArtist={(id) => openCatalog({ kind: 'artist', id, query: null })}
        onToast={showToast}
      />
    {:else if view === 'yandex'}
      <Home
        onPlay={handlePlayTrack}
        onEnqueue={handleEnqueue}
        currentTrackId={currentTrackId}
        position={displayPosition}
        onOpenSetup={() => navigate('setup')}
        onOpenDownloads={() => navigate('downloads')}
        onOpenPlaylist={(playlist) =>
          openCatalog({ kind: 'playlist', id: playlist.id, query: null })}
        onSearch={(query) => openCatalog({ kind: 'search', id: null, query })}
        onToast={showToast}
      />
    {:else}
      <div class="now-stack">
        <NowHero
          state={$player}
          track={$player?.track ?? null}
          position={displayPosition}
          onToggleShuffle={handleToggleShuffle}
          onCycleRepeat={handleCycleRepeat}
          onPlay={handlePlayPause}
          onPause={handlePlayPause}
          onNext={handleNext}
          onPrevious={handlePrevious}
          onSeek={handleSeek}
          onEnqueue={handleEnqueue}
          onKaraoke={() => (karaokeOpen = true)}
          onToast={showToast}
        />
        {#if currentTrackId}
          <LyricsPanel
            trackId={currentTrackId}
            position={displayPosition}
            onSeek={handleSeek}
            onToast={showToast}
          />
        {/if}
        <Shelf
          tracks={$shelf}
          total={$shelfTotal}
          mode={$serverMode}
          currentTrackId={currentTrackId}
          onPlay={handlePlayTrack}
          onEnqueue={handleEnqueue}
        />
        <HistoryShelf
          currentTrackId={currentTrackId}
          onPlay={handlePlayTrack}
          onEnqueue={handleEnqueue}
          onToast={showToast}
        />
        <OfflineShelf
          onPlay={handlePlayTrack}
          onEnqueue={handleEnqueue}
          currentTrackId={currentTrackId}
        />
      </div>
    {/if}
  </PageShell>

  <div slot="dock">
    <NowDock
      player={$player}
      position={displayPosition}
      queue={$queue}
      booting={!booted}
      queueOpen={queueOpen}
      onPlayPause={handlePlayPause}
      onPause={() => { if ($player?.status === 'playing') void handlePlayPause(); }}
      onNext={handleNext}
      onPrevious={handlePrevious}
      onToggleShuffle={handleToggleShuffle}
      onCycleRepeat={handleCycleRepeat}
      onSeek={handleSeek}
      onVolume={handleVolume}
      onToggleQueue={() => (queueOpen = !queueOpen)}
      onOpenNow={() => navigate('now')}
      onOpenZones={() => navigate('zones')}
      onOpenNetwork={() => navigate('network')}
      onToast={showToast}
    />
    <BottomNav
      section={section}
      systemPage={systemPage}
      onnavigate={navigateSection}
      onsystem={navigateSystem}
    />
  </div>

  <QueueDrawer
    slot="queue"
    open={queueOpen}
    queue={$queue}
    onClose={() => (queueOpen = false)}
    onRemove={handleRemove}
    onPlayTrack={handlePlayTrack}
    onMoveUp={(itemId) => handleQueueMove(itemId, -1)}
    onMoveDown={(itemId) => handleQueueMove(itemId, 1)}
    onClear={handleClearQueue}
  />
</AppShell>

<footer class="app-footer">
  <span>MusicNX {$serverVersion}</span>
</footer>
  {#if karaokeOpen}
    <KaraokeView
      track={$player?.track ?? null}
      position={displayPosition}
      onSeek={handleSeek}
      onClose={() => (karaokeOpen = false)}
    />
  {/if}

  <BrowserAudio
    playerState={$player}
    position={displayPosition}
    browserZone={browserZone}
    trackId={currentTrackId}
    volume={$player?.volume ?? null}
    onError={(message) => showToast(message, 'error')}
  />


  {#if $toastMessage}
    <div class="toast" class:error={$toastTone === 'error'} role="status" aria-live="polite">
      <span>{$toastMessage}</span>
      <button type="button" aria-label="Закрыть уведомление" onclick={() => toastMessage.set(null)}>
        <Icon name="close" size={14} />
      </button>
    </div>
  {/if}
<style>
  .now-stack {
    display: flex;
    flex-direction: column;
    gap: var(--section-gap);
    min-width: 0;
  }

  .boot-stack {
    display: flex;
    flex-direction: column;
    gap: var(--section-gap);
    min-width: 0;
  }

  .skeleton-block {
    border-radius: 26px;
    border: 1px solid var(--border);
    background:
      linear-gradient(
        100deg,
        rgba(255, 255, 255, 0.03) 25%,
        rgba(255, 255, 255, 0.075) 45%,
        rgba(255, 255, 255, 0.03) 65%
      );
    background-size: 220% 100%;
    animation: boot-shimmer 1.5s ease-in-out infinite;
  }

  .boot-hero {
    min-height: 286px;
  }

  .boot-shelf {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 16px;
  }

  .boot-card {
    min-height: 240px;
    border-radius: 20px;
  }

  @keyframes boot-shimmer {
    0% { background-position: 120% 0; }
    100% { background-position: -40% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-block { animation: none; }
  }

  .ap-portal {
    margin-bottom: 16px;
    padding: 16px 18px;
    border: 1px solid color-mix(in srgb, var(--accent-2) 32%, transparent);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--accent-2) 9%, transparent);
  }

  .ap-portal strong { display: block; font-size: var(--text-body); }
  .ap-portal p { margin: 5px 0 0; color: var(--text-soft); font-size: var(--text-meta); }

  .app-footer {
    position: relative;
    z-index: 1;
    /* The footer paints above the isolated AppShell stacking context; without
     * this it intercepts clicks on the fixed dock (queue/volume buttons). */
    pointer-events: none;
    max-width: var(--page-max);
    margin: 0 auto;
    padding: 0 var(--page-gutter) calc(var(--dock-height) + var(--bnav-height) + 18px);
    color: var(--text-muted);
    font-size: 0.69rem;
    letter-spacing: 0.06em;
  }

  .toast {
    position: fixed;
    z-index: 60;
    left: 50%;
    bottom: calc(var(--dock-height) + var(--bnav-height) + 16px);
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 12px;
    max-width: min(560px, calc(100vw - 32px));
    padding: 10px 11px 10px 15px;
    border: 1px solid color-mix(in srgb, var(--accent-2) 30%, transparent);
    border-radius: var(--radius-sm);
    background: var(--glass-3);
    color: var(--text);
    box-shadow: var(--shadow-soft);
    font-size: var(--text-meta);
    animation: toast-in 160ms var(--ease-out);
  }

  .toast span { overflow-wrap: anywhere; }

  .toast.error {
    border-color: color-mix(in srgb, var(--danger) 30%, transparent);
    color: #fecdd3;
  }

  .toast button {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border-radius: 7px;
    color: inherit;
    opacity: 0.7;
  }

  .toast button:hover { opacity: 1; background: rgba(255, 255, 255, 0.08); }

  @keyframes toast-in {
    from { opacity: 0; transform: translate(-50%, 8px); }
    to { opacity: 1; transform: translate(-50%, 0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .toast { animation: none; }
  }

  @media (prefers-reduced-transparency: reduce) {
    .toast { backdrop-filter: none; background: var(--bg-elevated); }
  }
</style>
