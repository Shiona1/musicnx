<script lang="ts">
  import { onDestroy } from 'svelte';
  import { fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import CoverArt from '../CoverArt.svelte';
  import Icon from '../Icon.svelte';
  import IconButton from './IconButton.svelte';
  import StatusPill from './StatusPill.svelte';
  import { artistNames, formatDuration, formatTime } from '../../lib/art';
  import { zonesDegraded } from '../../lib/zonesStores';
  import type { PlayerState, QueueState } from '../../lib/types';

  export let player: PlayerState | null = null;
  export let position = 0;
  export let queue: QueueState | null = null;
  export let busy = false;
  export let booting = false;
  export let queueOpen = false;
  export let onPlayPause: () => void = () => {};
  export let onPause: () => void = () => {};
  export let onNext: () => void = () => {};
  export let onPrevious: () => void = () => {};
  export let onSeek: (positionMs: number) => void = () => {};
  export let onVolume: (volume: number) => void = () => {};
  export let onToggleShuffle: () => void = () => {};
  export let onCycleRepeat: () => void = () => {};
  export let onToggleQueue: () => void = () => {};
  export let onOpenNow: () => void = () => {};
  export let onOpenZones: () => void = () => {};
  export let onOpenNetwork: () => void = () => {};
  export let onToast: (message: string) => void = () => {};

  let menuOpen = false;
  let dockEl: HTMLElement | null = null;
  let sleepMinutes = 0;
  let sleepLeft = 0;
  let sleepTimer: ReturnType<typeof setInterval> | null = null;
  let seekPreview: number | null = null;

  $: track = player?.track ?? null;
  $: duration = player?.duration_ms || track?.duration_ms || 0;
  $: isPlaying = player?.status === 'playing';
  $: isBuffering = player?.status === 'buffering';
  $: volume = player?.volume ?? 70;
  $: queueCount = queue?.total ?? 0;
  $: coverTitle = track?.title ?? 'MusicNX';
  $: repeatMode = player?.repeat ?? 'off';
  $: repeatLabel =
    repeatMode === 'one'
      ? 'Повтор: один трек'
      : repeatMode === 'all'
        ? 'Повтор: весь плейлист'
        : 'Повтор выключен';
  $: shownPosition = seekPreview ?? Math.min(position, duration || position);

  onDestroy(() => {
    if (sleepTimer) clearInterval(sleepTimer);
  });

  function seek(event: Event): void {
    const value = Number((event.currentTarget as HTMLInputElement).value);
    seekPreview = null;
    onSeek(value);
  }

  function previewSeek(event: Event): void {
    seekPreview = Number((event.currentTarget as HTMLInputElement).value);
  }

  function clock(ms: number): string {
    return ms <= 0 ? '0:00' : formatTime(ms);
  }

  function setVolume(event: Event): void {
    onVolume(Number((event.currentTarget as HTMLInputElement).value));
  }

  function setSleep(event: Event): void {
    sleepMinutes = Number((event.currentTarget as HTMLSelectElement).value);
    if (sleepTimer) clearInterval(sleepTimer);
    sleepTimer = null;
    sleepLeft = sleepMinutes * 60;
    if (sleepMinutes > 0) {
      sleepTimer = setInterval(() => {
        sleepLeft -= 1;
        if (sleepLeft <= 0) {
          if (sleepTimer) clearInterval(sleepTimer);
          sleepTimer = null;
          sleepMinutes = 0;
          sleepLeft = 0;
          onPause();
          onToast('Таймер сна: воспроизведение приостановлено.');
        }
      }, 1000);
    }
  }

  function leftText(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function reducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function menuIn(node: HTMLElement) {
    return fly(node, { y: 5, duration: reducedMotion() ? 0 : 150, easing: cubicOut });
  }

  function menuOut(node: HTMLElement) {
    return fly(node, { y: 3, duration: reducedMotion() ? 0 : 110, easing: cubicOut });
  }

  function toggleQueue(): void {
    menuOpen = false;
    onToggleQueue();
  }

  function closeMenuOutside(event: PointerEvent): void {
    if (!menuOpen || dockEl === null) return;
    if (!dockEl.contains(event.target as Node)) menuOpen = false;
  }

  function closeMenuEscape(event: KeyboardEvent): void {
    if (event.key === 'Escape') menuOpen = false;
  }
</script>

<svelte:window onpointerdown={closeMenuOutside} onkeydown={closeMenuEscape} />

<div class="now-dock-shell" bind:this={dockEl}>
<footer class="now-dock material-chrome" aria-label="Мини-плеер">
  <div class="dock-progress" aria-hidden="true">
    <span style={`width:${duration > 0 ? Math.min(100, (position / duration) * 100) : 0}%`}></span>
  </div>

  <button class="dock-track" type="button" onclick={onOpenNow} aria-label="Открыть «Сейчас»">
    <span class="dock-cover">
      <CoverArt url={track?.cover_url} title={coverTitle} alt="" size={128} />
      {#if isPlaying}
        <span class="mini-eq" aria-hidden="true"><i></i><i></i><i></i></span>
      {/if}
    </span>
    <span class="dock-copy">
      <span class="dock-title" title={track?.title ?? ''}>
        {booting ? 'Загрузка…' : track?.title ?? 'Ничего не играет'}
      </span>
      <span class="dock-artist">
        {booting ? 'Подключаемся к серверу' : track ? artistNames(track.artists) : 'Выберите трек'}
      </span>
    </span>
  </button>

  <div class="dock-transport">
    <IconButton label="Перемешать" title="Перемешать" active={player?.shuffle ?? false} onclick={onToggleShuffle}>
      <Icon name="shuffle" size={17} />
    </IconButton>
    <IconButton label="Предыдущий трек" disabled={busy || !(player?.can_previous ?? false)} onclick={onPrevious}>
      <Icon name="previous" size={18} />
    </IconButton>
    <button
      class="dock-play"
      type="button"
      aria-label={isPlaying ? 'Пауза' : 'Играть'}
      disabled={busy || (!isPlaying && !(player?.can_play ?? false))}
      onclick={() => { if (isPlaying || isBuffering) onPause(); else onPlayPause(); }}
    >
      {#if isBuffering}<span class="spinner" aria-hidden="true"></span>{:else}<Icon name={isPlaying ? 'pause' : 'play'} size={21} />{/if}
    </button>
    <IconButton label="Следующий трек" disabled={busy || !(player?.can_next ?? false)} onclick={onNext}>
      <Icon name="next" size={18} />
    </IconButton>
    <IconButton label={repeatLabel} title={repeatLabel} active={repeatMode !== 'off'} onclick={onCycleRepeat}>
      <Icon name={repeatMode === 'one' ? 'repeat-one' : 'repeat'} size={17} />
    </IconButton>
  </div>

  <div class="dock-seek">
    <span class="dock-time">{clock(shownPosition)}</span>
    <input
      type="range"
      min="0"
      max={duration || 1}
      step="1000"
      value={shownPosition}
      aria-label="Позиция воспроизведения"
      disabled={!track || duration === 0}
      oninput={previewSeek}
      onchange={seek}
    />
    <span class="dock-time">{clock(duration)}</span>
  </div>

  <div class="dock-volume">
    <Icon name="volume" size={15} />
    <input
      type="range"
      min="0"
      max="100"
      value={volume}
      aria-label="Громкость"
      disabled={busy}
      onchange={setVolume}
    />
    <span>{volume}</span>
  </div>

  <div class="dock-actions">
    {#if $zonesDegraded}
      <StatusPill label="audio mock" tone="warning" title="Real-режим: роутер откатился на mock" />
    {/if}

    <IconButton
      label="Очередь"
      title="Очередь"
      id="queue-trigger"
      expanded={queueOpen}
      controls="queue-drawer"
      onclick={toggleQueue}
    >
      <span class="queue-glyph"><Icon name="queue" size={18} />{#if queueCount > 0}<b>{queueCount}</b>{/if}</span>
    </IconButton>

    <div class="dock-menu-wrap">
      <IconButton
        label="Быстрые настройки"
        title="Быстрые настройки"
        active={menuOpen}
        expanded={menuOpen}
        controls="now-quick-menu"
        onclick={() => (menuOpen = !menuOpen)}
      >
        <Icon name="dots" size={18} />
      </IconButton>
    </div>
  </div>
</footer>

{#if menuOpen}
  <div class="dock-menu material-overlay" id="now-quick-menu" role="menu" aria-label="Быстрые настройки" in:menuIn out:menuOut>
    <button type="button" role="menuitem" onclick={() => { menuOpen = false; onOpenZones(); }}>Зоны и Bluetooth</button>
    <button type="button" role="menuitem" onclick={() => { menuOpen = false; onOpenNetwork(); }}>Сеть и Wi-Fi</button>
    <label class="dock-sleep">
      <span>Громкость</span>
      <input type="range" min="0" max="100" value={volume} aria-label="Громкость" onchange={setVolume} />
    </label>
    <label class="dock-sleep">
      <span>Таймер сна</span>
      <select value={sleepMinutes} onchange={setSleep} aria-label="Таймер сна">
        <option value={0}>выкл</option>
        <option value={15}>15 мин</option>
        <option value={30}>30 мин</option>
        <option value={60}>60 мин</option>
      </select>
    </label>
    {#if sleepLeft > 0}<p class="sleep-left">осталось {leftText(sleepLeft)}</p>{/if}
  </div>
{/if}
</div>
<style>
  .now-dock {
    position: relative;
    display: grid;
    grid-template-columns: minmax(160px, 1.2fr) auto minmax(180px, 1.5fr) 150px auto;
    align-items: center;
    gap: 14px;
    width: 100%;
    min-height: var(--dock-height);
    padding: 10px max(16px, calc((100vw - var(--page-max)) / 2 + var(--page-gutter))) calc(10px + env(safe-area-inset-bottom, 0px));
  }

  .now-dock-shell { position: relative; width: 100%; }

  .now-dock.material-chrome {
    border: 0;
    border-top: 1px solid var(--material-edge);
    border-radius: 0;
    overflow: visible;
    background-image: radial-gradient(
      120% 180% at 18% 100%,
      color-mix(in srgb, var(--cover-accent) 16%, transparent),
      transparent 64%
    );
    box-shadow:
      0 -26px 64px -34px rgba(0, 0, 0, 0.96),
      var(--material-inner-top);
  }

  .dock-progress {
    position: absolute;
    top: -1px;
    left: 0;
    right: 0;
    height: 2px;
    background: rgba(255, 255, 255, 0.06);
  }

  .dock-progress span {
    display: block;
    height: 100%;
    background: var(--cover-accent);
    transition: width 220ms linear;
  }

  .dock-track {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    color: var(--text);
    text-align: left;
  }

  .dock-cover {
    position: relative;
    width: 54px;
    height: 54px;
    flex: none;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid var(--stroke);
    background: var(--bg-elevated);
  }

  .mini-eq {
    position: absolute;
    right: 5px;
    bottom: 5px;
    display: inline-flex;
    align-items: flex-end;
    gap: 1.5px;
    height: 9px;
    padding: 2px 3px;
    border-radius: 4px;
    background: rgba(4, 7, 12, 0.7);
  }

  .mini-eq i { width: 2px; height: 100%; background: var(--cover-accent); animation: dock-eq 0.9s ease-in-out infinite; }
  .mini-eq i:nth-child(2) { animation-delay: 0.15s; }
  .mini-eq i:nth-child(3) { animation-delay: 0.3s; }

  .dock-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
  .dock-title {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    font-size: var(--text-card);
    font-weight: 600;
  }
  .dock-artist {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    color: var(--text-muted);
    font-size: var(--text-meta);
  }

  .dock-transport { display: flex; align-items: center; gap: 2px; }

  .dock-play {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border-radius: 50%;
    color: #080b12;
    background: var(--cover-accent);
    transition: transform var(--motion-fast) var(--ease-out), background var(--motion-fast) ease;
  }

  .dock-play:hover:not(:disabled) { transform: scale(1.04); background: color-mix(in srgb, var(--cover-accent) 82%, white); }
  .dock-play:disabled { opacity: 0.4; }

  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(0, 0, 0, 0.25);
    border-top-color: #080b12;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  .dock-seek { display: flex; align-items: center; gap: 8px; min-width: 0; }

  .dock-time {
    color: var(--text-muted);
    font-size: 0.65rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .dock-seek input {
    width: 100%;
    accent-color: var(--cover-accent);
  }

  .dock-volume { display: flex; align-items: center; gap: 7px; color: var(--text-muted); }
  .dock-volume input { width: 74px; accent-color: var(--cover-accent); }
  .dock-volume span { font-size: 0.68rem; font-variant-numeric: tabular-nums; color: var(--text-soft); }

  .dock-actions { display: flex; align-items: center; gap: 4px; justify-content: flex-end; }


  .queue-glyph { position: relative; display: inline-grid; place-items: center; }
  .queue-glyph b {
    position: absolute;
    top: -6px;
    right: -9px;
    min-width: 14px;
    height: 14px;
    padding: 0 3px;
    border-radius: 999px;
    background: var(--cover-accent);
    color: #080b12;
    font-size: 0.56rem;
    line-height: 14px;
  }

  .dock-menu-wrap { position: relative; }

  .dock-menu {
    position: absolute;
    z-index: 2;
    right: max(16px, calc((100vw - var(--page-max)) / 2 + var(--page-gutter)));
    bottom: calc(100% + 10px);
    width: 210px;
    padding: 8px;
  }

  .dock-menu button {
    display: block;
    width: 100%;
    padding: 8px 10px;
    border-radius: var(--radius-xs);
    color: var(--text-soft);
    text-align: left;
    font-size: var(--text-meta);
  }

  .dock-menu button:hover { color: var(--text); background: var(--surface-hover); }

  .dock-sleep {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 4px;
    padding: 8px 10px;
    color: var(--text-muted);
    font-size: var(--text-meta);
  }

  .dock-sleep select {
    border: 1px solid var(--stroke);
    border-radius: var(--radius-xs);
    padding: 3px 5px;
    background: var(--surface);
    color: var(--text);
    font-size: var(--text-meta);
  }

  .dock-sleep input[type='range'] {
    width: 104px;
    accent-color: var(--cover-accent);
  }

  .sleep-left {
    margin: 4px 10px 2px;
    color: var(--text-muted);
    font-size: var(--text-micro);
  }

  @keyframes dock-eq {
    0%, 100% { transform: scaleY(0.35); }
    50% { transform: scaleY(1); }
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 1100px) {
    .now-dock { grid-template-columns: minmax(150px, 1fr) auto minmax(130px, 1fr) auto; }
    .dock-volume { display: none; }
  }

  /* The bottom navigation owns the safe area below 900px. */
  @media (max-width: 900px) {
    .now-dock { padding-bottom: 10px; }
  }

  @media (max-width: 760px) {
    .now-dock {
      grid-template-columns: minmax(0, 1fr) auto auto;
      grid-template-areas:
        "track transport actions"
        "seek seek seek";
      gap: 6px 8px;
      padding: 8px 12px 6px;
    }
    .dock-track { grid-area: track; }
    .dock-transport { grid-area: transport; }
    .dock-actions { grid-area: actions; }
    /* Seek stays reachable with a thumb on phones; volume lives in the
       quick menu next to the sleep timer. */
    .dock-seek {
      grid-area: seek;
      display: flex;
      padding-top: 2px;
    }
    .dock-actions > :global(.status-pill) { display: none; }
    .dock-volume { display: none; }
    .dock-cover { width: 44px; height: 44px; border-radius: 10px; }
    .dock-copy .dock-artist { display: none; }
    .dock-play { width: 44px; height: 44px; }
    .dock-title { font-size: 0.8rem; }
  }

  @media (prefers-reduced-motion: reduce) {
    .now-dock *, .now-dock *::before, .now-dock *::after { animation: none !important; transition: none !important; }
  }
</style>
