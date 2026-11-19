<script lang="ts">
  import Icon from './Icon.svelte';
  import CoverArt from './CoverArt.svelte';
  import DownloadButton from './DownloadButton.svelte';
  import {
    artistNames,
    coverGradient,
    coverInitials,
    coverUrlAt,
    formatTime,
  } from '../lib/art';
  import type { PlayerState, Track } from '../lib/types';

  /**
   * One full-size "now playing" stage for local and Yandex tracks. The App owns
   * the single WS connection and feeds the authoritative state; this component
   * only presents it.
   */
  export let state: PlayerState | null = null;
  /** Optional explicit track (e.g. a preview); defaults to state.track. */
  export let track: Track | null = null;
  export let position = 0;
  export let busy = false;
  export let onPlay: () => void;
  export let onPause: () => void;
  export let onNext: () => void;
  export let onPrevious: () => void;
  export let onToggleShuffle: () => void = () => {};
  export let onCycleRepeat: () => void = () => {};
  export let onSeek: (positionMs: number) => void;
  export let onEnqueue: ((trackId: number) => void) | null = null;
  export let onKaraoke: (() => void) | null = null;
  export let onToast: ((message: string) => void) | null = null;

  let seekPreview: number | null = null;

  $: current = track ?? state?.track ?? null;
  $: duration = state?.duration_ms || current?.duration_ms || 0;
  $: isPlaying = state?.status === 'playing';
  $: isBuffering = state?.status === 'buffering';
  $: preview = current?.preview === true;
  $: shownPosition = seekPreview ?? Math.min(position, duration || position);
  $: progressPercent = duration > 0 ? Math.min(100, (shownPosition / duration) * 100) : 0;
  $: statusLabel =
    state?.status === 'playing'
      ? 'Играет'
      : state?.status === 'paused'
        ? 'Пауза'
        : state?.status === 'buffering'
          ? 'Буферизация'
          : state?.status === 'error'
            ? 'Ошибка'
            : 'Остановлено';
  $: isYandex = current?.source === 'yandex';
  $: repeatMode = state?.repeat ?? 'off';
  $: repeatLabel =
    repeatMode === 'one'
      ? 'Повтор: один трек'
      : repeatMode === 'all'
        ? 'Повтор: весь плейлист'
        : 'Повтор выключен';
  $: isError = state?.status === 'error';
  $: eyebrow = isError
    ? isYandex
      ? 'Яндекс Музыка · ошибка'
      : 'Ошибка воспроизведения'
    : isYandex
      ? 'Яндекс Музыка · сейчас играет'
      : 'Сейчас играет';
  $: errorText = isError ? describePlaybackError(state) : null;
  $: coverSeed = current ? `${current.title}|${current.album?.title ?? ''}` : 'MusicNX';
  $: largeCover = current ? coverUrlAt(current.cover_url, 1000) : null;
  $: coverStyle = largeCover
    ? `background-image: url('${largeCover}'); background-size: cover; background-position: center;`
    : `background-image: ${coverGradient(coverSeed)};`;

  function handleSeekInput(event: Event): void {
    seekPreview = Number((event.currentTarget as HTMLInputElement).value);
  }

  function handleSeekCommit(event: Event): void {
    const value = Number((event.currentTarget as HTMLInputElement).value);
    seekPreview = null;
    onSeek(value);
  }

  function enqueueCurrent(): void {
    if (current && onEnqueue) onEnqueue(current.id);
  }

  /**
   * Backend error codes are stable machine tokens; the Russian explanation is
   * chosen here so the player never shows a bare "Ошибка" with no way forward.
   */
  function describePlaybackError(state: PlayerState | null): string {
    switch (state?.error_code) {
      case 'cdn_unreachable':
      case 'stream_unavailable':
        return 'CDN Яндекса недоступен из сети Raspberry Pi. Проверьте провайдера/VPN: офлайн-треки с меткой «Офлайн» играют. Нажмите ▶, чтобы повторить.';
      case 'source_unreachable':
        return 'Источник не отвечает: сервер не смог подключиться к CDN Яндекса. Проверьте сеть или VPN на Pi — офлайн-треки играют. Нажмите ▶, чтобы повторить.';
      case 'cannot_start_playback':
        return 'Не удалось начать воспроизведение. Нажмите ▶, чтобы повторить.';
      case 'audio_backend_error':
        return 'Аудиодвижок mpv сообщил об ошибке. Проверьте, что mpv запущен, и повторите.';
      default:
        return 'Трек не воспроизвёлся. Нажмите ▶, чтобы повторить.';
    }
  }
</script>

<section class="yandex-now material-content" aria-label={isYandex ? 'Сейчас играет трек Яндекс Музыки' : 'Сейчас играет трек'}>
  <div class="inner">
    <div class="art-wrap">
    <div class="glow" style={coverStyle} aria-hidden="true"></div>
    <div class="art" style={coverStyle}>
      <CoverArt
        url={current?.cover_url}
        title={current?.title ?? 'MusicNX'}
        alt={current ? `Обложка «${current.title}»` : ''}
        size={1000}
        eager
      />
      {#if !current}
        <span class="idle-icon" aria-hidden="true"><Icon name="music" size={46} /></span>
      {/if}
      {#if preview}
        <span class="preview-badge" title="Звучит 30-секундное превью">30 сек</span>
      {/if}
      {#if isPlaying || isBuffering}
        <span class="playing-badge" aria-label={statusLabel}>
          <span class="equalizer" aria-hidden="true"><span></span><span></span><span></span></span>
          {isBuffering ? '…' : 'LIVE'}
        </span>
      {/if}
    </div>
  </div>

  <div class="info">
    <span class="eyebrow">{eyebrow}</span>
    <h1 aria-live="polite">{current?.title ?? 'Ничего не играет'}</h1>
    <p class="artists">
      {current
        ? artistNames(current.artists)
        : 'Выберите трек на полке или в Яндекс Музыке'}
    </p>

    <div class="meta-chips">
      <span class="status-chip" class:live={isPlaying} class:error={state?.status === 'error'}>
        <span class="status-dot" aria-hidden="true"></span>{statusLabel}
      </span>
      {#if current}
        {#if isYandex}<span class="chip source">Яндекс</span>{/if}
        {#if current.album?.title}
          <span class="chip album">{current.album.title}{current.album.year ? ` · ${current.album.year}` : ''}</span>
        {/if}
        <span class="chip">{formatTime(duration)}</span>
      {/if}
    </div>

    {#if preview}
      <p class="preview-note">
        Звучит 30-секундное превью; полный трек доступен после загрузки в медиатеку.
      </p>
    {/if}

    {#if errorText}
      <p class="playback-error" role="alert">{errorText}</p>
    {/if}

    <div class="progress-block">
      <input
        class="slider"
        type="range"
        min="0"
        max={duration || 1}
        step="100"
        value={shownPosition}
        style={`--progress: ${progressPercent}%`}
        aria-label="Позиция воспроизведения"
        aria-valuetext={`${formatTime(shownPosition)} из ${formatTime(duration)}`}
        disabled={!current || duration === 0}
        oninput={handleSeekInput}
        onchange={handleSeekCommit}
      />
      <div class="times" aria-hidden="true">
        <span>{formatTime(shownPosition)}</span>
        <span>{formatTime(duration)}</span>
      </div>
    </div>

    <div class="controls">
      <button
        class="control"
        type="button"
        aria-label="Перемешать"
        aria-pressed={state?.shuffle ?? false}
        class:active={state?.shuffle ?? false}
        onclick={onToggleShuffle}
      >
        <Icon name="shuffle" size={20} />
      </button>
      <button
        class="control"
        type="button"
        aria-label="Предыдущий трек"
        disabled={busy || !(state?.can_previous ?? false)}
        onclick={onPrevious}
      >
        <Icon name="previous" size={22} />
      </button>

      <button
        class="control primary"
        type="button"
        aria-label={isPlaying ? 'Пауза' : 'Играть'}
        disabled={busy || !current}
        onclick={isPlaying ? onPause : onPlay}
      >
        {#if isBuffering}
          <span class="spinner" aria-hidden="true"></span>
        {:else}
          <Icon name={isPlaying ? 'pause' : 'play'} size={26} />
        {/if}
      </button>

      <button
        class="control"
        type="button"
        aria-label="Следующий трек"
        disabled={busy || !(state?.can_next ?? false)}
        onclick={onNext}
      >
        <Icon name="next" size={22} />
      </button>
      <button
        class="control"
        type="button"
        aria-label={repeatLabel}
        title={repeatLabel}
        aria-pressed={repeatMode !== 'off'}
        class:active={repeatMode !== 'off'}
        onclick={onCycleRepeat}
      >
        <Icon name={repeatMode === 'one' ? 'repeat-one' : 'repeat'} size={20} />
      </button>

      {#if onEnqueue && current}
        <button
          class="control ghost"
          type="button"
          aria-label={`Добавить «${current.title}» в очередь`}
          title="В очередь"
          disabled={busy}
          onclick={enqueueCurrent}
        >
          <Icon name="queue" size={20} />
        </button>
      {/if}
    </div>
    <div class="hero-tools">
      {#if current && isYandex}
        <DownloadButton trackId={current.id} label="Скачать" {onToast} disabled={busy} />
      {/if}
      {#if current && onKaraoke}
        <button type="button" class="text-mode" onclick={() => onKaraoke?.()}>Текст · L</button>
      {/if}
    </div>
    </div>
  </div>
</section>
<style>
  .hero-tools {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 8px;
  }

  .text-mode {
    align-self: flex-start;
    padding: 7px 12px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-soft);
    font-size: var(--text-meta);
    font-weight: 600;
    transition: background var(--motion-fast) ease, color var(--motion-fast) ease;
  }

  .text-mode:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  .yandex-now {
    container-type: inline-size;
    padding: clamp(18px, 2.2vw, 28px);
    border-radius: 26px;
  }

  .inner {
    display: grid;
    grid-template-columns: minmax(200px, 260px) minmax(0, 1fr);
    gap: clamp(18px, 2.6vw, 34px);
    align-items: start;
  }

  .art-wrap {
    position: relative;
    align-self: start;
  }

  .glow {
    position: absolute;
    inset: 8% 4% 0;
    filter: blur(40px);
    opacity: 0.5;
    border-radius: 30px;
    transform: translateZ(0);
  }

  .art {
    position: relative;
    aspect-ratio: 1;
    border-radius: 22px;
    display: grid;
    place-items: center;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.18);
  }

  .art::after {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at 30% 20%, rgba(255, 255, 255, 0.16), transparent 60%);
  }

  .idle-icon {
    color: rgba(255, 255, 255, 0.45);
  }

  .preview-badge {
    position: absolute;
    z-index: 2;
    left: 12px;
    top: 12px;
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    color: #fde68a;
    background: rgba(5, 8, 14, 0.64);
    border: 1px solid rgba(253, 230, 138, 0.38);
  }

  .playing-badge {
    position: absolute;
    z-index: 2;
    right: 12px;
    bottom: 12px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 0.64rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    background: rgba(5, 8, 14, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.16);
  }

  .equalizer {
    display: inline-flex;
    align-items: flex-end;
    gap: 2px;
    height: 10px;
  }

  .equalizer span {
    width: 2px;
    height: 100%;
    background: var(--accent-2);
    transform-origin: bottom;
    animation: bump 0.9s ease-in-out infinite;
  }

  .equalizer span:nth-child(2) {
    animation-delay: 0.15s;
  }
  .equalizer span:nth-child(3) {
    animation-delay: 0.3s;
  }

  @keyframes bump {
    0%,
    100% {
      transform: scaleY(0.35);
    }
    50% {
      transform: scaleY(1);
    }
  }

  .info {
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 10px;
  }

  .eyebrow {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--accent-2);
  }

  h1 {
    margin: 0;
    font-size: clamp(1.4rem, 2.8vw, 2.2rem);
    line-height: 1.14;
    letter-spacing: -0.015em;
    overflow-wrap: anywhere;
  }

  .artists {
    margin: 0;
    color: var(--muted);
    font-size: clamp(0.9rem, 1.3vw, 1.02rem);
    overflow-wrap: anywhere;
  }

  .meta-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }

  .chip,
  .status-chip {
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
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status-chip.live {
    color: #6ee7b7;
    border-color: rgba(110, 231, 183, 0.3);
    background: rgba(16, 185, 129, 0.1);
  }

  .status-chip.error {
    color: var(--danger);
    border-color: rgba(248, 113, 113, 0.35);
    background: rgba(248, 113, 113, 0.1);
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
  }

  .chip.source {
    color: #c4b5fd;
    border-color: rgba(139, 92, 246, 0.32);
    background: rgba(139, 92, 246, 0.12);
  }

  .chip.album {
    color: #c4b5fd;
  }

  .preview-note {
    margin: 0;
    font-size: 0.74rem;
    color: #fcd34d;
  }

  .playback-error {
    margin: 0;
    padding: 9px 12px;
    border-radius: 12px;
    border: 1px solid rgba(248, 113, 113, 0.32);
    background: rgba(248, 113, 113, 0.08);
    color: #fecdd3;
    font-size: 0.76rem;
    line-height: 1.45;
  }

  .progress-block {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .slider {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 6px;
    border-radius: 999px;
    background: linear-gradient(
      to right,
      var(--accent-2) 0%,
      var(--accent) var(--progress, 0%),
      rgba(255, 255, 255, 0.12) var(--progress, 0%),
      rgba(255, 255, 255, 0.12) 100%
    );
    cursor: pointer;
  }

  .slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #f8fafc;
    border: 0;
    box-shadow: 0 0 0 4px rgba(34, 211, 238, 0.18);
    transition: transform 120ms ease;
  }

  .slider:hover::-webkit-slider-thumb {
    transform: scale(1.15);
  }

  .slider::-moz-range-thumb {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #f8fafc;
    border: 0;
  }

  .slider:disabled {
    cursor: default;
    opacity: 0.5;
  }

  .times {
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    font-size: 0.7rem;
    font-variant-numeric: tabular-nums;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .control {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    transition:
      transform 120ms ease,
      background 120ms ease;
  }

  .control:hover:not(:disabled) {
    background: var(--surface-hover);
    transform: translateY(-1px);
  }

  .control.primary {
    width: 58px;
    height: 58px;
    border: 0;
    background: var(--accent);
    color: #0b0715;
    box-shadow: 0 12px 30px -16px color-mix(in srgb, var(--accent) 75%, transparent);
  }

  .control.ghost {
    border-radius: 14px;
  }

  .control.active {
    color: var(--cover-accent);
    border-color: color-mix(in srgb, var(--cover-accent) 40%, transparent);
  }

  .spinner {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    border: 2px solid rgba(11, 7, 21, 0.3);
    border-top-color: #0b0715;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Container query keeps the panel usable inside a narrow column (queue side). */
  @container (max-width: 560px) {
    .inner {
      grid-template-columns: minmax(0, 1fr);
    }
    .art-wrap {
      max-width: 240px;
      width: 100%;
    }
  }

  @media (max-width: 860px) {
    .inner {
      grid-template-columns: minmax(0, 1fr);
    }
    .art-wrap {
      max-width: 240px;
      width: 100%;
    }
  }
</style>
