<script lang="ts">
  import CoverArt from './CoverArt.svelte';
  import Icon from './Icon.svelte';
  import { artistNames, availabilityLabel, formatDuration } from '../lib/art';
  import type { Track } from '../lib/types';

  export let tracks: Track[];
  export let total: number;
  export let mode: string;
  export let currentTrackId: number | null;
  export let busy = false;
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: (trackId: number) => void;

  let rail: HTMLUListElement | null = null;

  function scrollRail(direction: 1 | -1): void {
    rail?.scrollBy({ left: direction * Math.max(240, rail.clientWidth * 0.8), behavior: 'smooth' });
  }
</script>

<section class="shelf material-content" aria-labelledby="shelf-title">
  <div class="shelf-head">
    <div>
      <h2 id="shelf-title">Сейчас в библиотеке</h2>
      <p class="shelf-sub">
        {mode === 'mock' ? 'Локальный каталог' : `Треки · ${total}`}
      </p>
    </div>
    <div class="rail-actions">
      <span class="count">{tracks.length} из {total}</span>
      <button type="button" class="rail-arrow" aria-label="Прокрутить полку влево" onclick={() => scrollRail(-1)}>
        <Icon name="chevron-left" size={16} />
      </button>
      <button type="button" class="rail-arrow" aria-label="Прокрутить полку вправо" onclick={() => scrollRail(1)}>
        <Icon name="chevron-right" size={16} />
      </button>
    </div>
  </div>

  {#if tracks.length === 0}
    <div class="empty">
      <Icon name="music" size={30} />
      <div>
        <strong>Библиотека пуста</strong>
        <p>Локальные файлы и онлайн-треки появятся здесь после импорта или подключения Яндекса.</p>
      </div>
    </div>
  {:else}
    <ul class="cards" bind:this={rail}>
      {#each tracks as track (track.id)}
        <li class="card" class:active={track.id === currentTrackId}>
          <div class="art">
            <CoverArt
              url={track.cover_url}
              title={track.title}
              alt={`Обложка «${track.title}»`}
              size={300}
            />
            <span class="art-shade" aria-hidden="true"></span>
            <span class="availability" class:ok={track.availability === 'online' || track.availability === 'offline'}>
              {availabilityLabel(track.availability)}
            </span>
            {#if track.id === currentTrackId}
              <span class="live" aria-label="Сейчас играет">
                <span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>
              </span>
            {/if}
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
            <button
              class="play-hit"
              type="button"
              aria-label={`Воспроизвести «${track.title}»`}
              disabled={busy}
              onclick={() => onPlay(track.id)}
            >
              <span class="play-disc" aria-hidden="true"><Icon name="play" size={18} /></span>
              <span class="overlay">
                <span class="title" title={track.title}>{track.title}</span>
                <span class="artist" title={artistNames(track.artists)}>{artistNames(track.artists)}</span>
                <span class="duration">{formatDuration(track.duration_ms)}</span>
              </span>
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .shelf {
    border-radius: 28px;
    padding: clamp(18px, 2.2vw, 26px);
  }

  .shelf-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 16px;
  }

  h2 {
    margin: 0;
    font-size: 1.05rem;
    letter-spacing: 0.01em;
  }

  .shelf-sub {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 0.76rem;
  }

  .rail-actions {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .count {
    color: var(--muted);
    font-size: 0.72rem;
    margin-right: 4px;
  }

  .rail-arrow {
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.04);
    color: var(--muted);
    transition: transform 140ms ease, background 140ms ease, color 140ms ease;
  }

  .rail-arrow:hover {
    transform: translateY(-1px);
    background: rgba(255, 255, 255, 0.09);
    color: var(--text);
  }

  .cards {
    list-style: none;
    margin: 0;
    padding: 2px 2px 10px;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: clamp(196px, 22vw, 250px);
    gap: 16px;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    scrollbar-width: thin;
    mask-image: linear-gradient(to right, transparent, #000 12px, #000 calc(100% - 12px), transparent);
  }

  .card {
    scroll-snap-align: start;
    min-width: 0;
  }

  .art {
    position: relative;
    aspect-ratio: 1 / 1.12;
    border-radius: 22px;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.1);
    background: #111827;
    box-shadow: 0 18px 38px -24px rgba(0, 0, 0, 0.9);
    transition: transform 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
  }

  .card:hover .art,
  .card:focus-within .art {
    transform: translateY(-4px);
    border-color: rgba(255, 255, 255, 0.24);
    box-shadow: 0 26px 54px -24px rgba(0, 0, 0, 0.95);
  }

  .card.active .art {
    border-color: rgba(34, 211, 238, 0.55);
    box-shadow: 0 0 0 1px rgba(34, 211, 238, 0.25), 0 26px 54px -24px rgba(0, 0, 0, 0.95);
  }

  .art-shade {
    position: absolute;
    inset: 0;
    background:
      linear-gradient(180deg, rgba(4, 7, 13, 0.06) 30%, rgba(4, 7, 13, 0.2) 58%, rgba(4, 7, 13, 0.9) 100%),
      radial-gradient(circle at 20% 0%, rgba(255, 255, 255, 0.14), transparent 48%);
    pointer-events: none;
  }

  .play-hit {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0;
    text-align: left;
    color: var(--text);
  }

  .play-disc {
    position: absolute;
    z-index: 2;
    right: 14px;
    bottom: 88px;
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: rgba(8, 12, 20, 0.7);
    opacity: 0;
    transform: translateY(6px);
    transition: opacity 160ms ease, transform 160ms ease;
  }

  .play-hit:hover .play-disc,
  .play-hit:focus-visible .play-disc {
    opacity: 1;
    transform: translateY(0);
  }

  .overlay {
    position: absolute;
    z-index: 2;
    left: 15px;
    right: 15px;
    bottom: 13px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .title {
    font-size: 0.95rem;
    font-weight: 680;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 2px 14px rgba(0, 0, 0, 0.55);
  }

  .artist {
    color: rgba(238, 242, 251, 0.72);
    font-size: 0.76rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .duration {
    color: rgba(238, 242, 251, 0.58);
    font-size: 0.68rem;
    margin-top: 2px;
  }

  .availability {
    position: absolute;
    z-index: 3;
    left: 12px;
    top: 12px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(8, 12, 20, 0.58);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #fca5a5;
    font-size: 0.62rem;
    letter-spacing: 0.03em;
  }

  .availability.ok {
    color: #b9f6d0;
  }

  .live {
    position: absolute;
    z-index: 3;
    right: 12px;
    top: 12px;
    display: inline-flex;
    padding: 5px 8px;
    border-radius: 999px;
    background: rgba(8, 12, 20, 0.62);
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

  .eq i:nth-child(2) { animation-delay: 0.15s; }
  .eq i:nth-child(3) { animation-delay: 0.3s; }

  @keyframes bounce {
    0%, 100% { transform: scaleY(0.35); }
    50% { transform: scaleY(1); }
  }

  .enqueue {
    position: absolute;
    z-index: 4;
    right: 12px;
    top: 44px;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    background: rgba(8, 12, 20, 0.56);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: var(--text);
    opacity: 0;
    transform: translateY(-4px);
    transition: opacity 160ms ease, transform 160ms ease;
  }

  .card:hover .enqueue,
  .card:focus-within .enqueue {
    opacity: 1;
    transform: translateY(0);
  }

  .empty {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 22px;
    border-radius: 18px;
    border: 1px dashed var(--border);
    color: var(--muted);
  }

  .empty strong {
    display: block;
    color: var(--text);
    margin-bottom: 2px;
  }

  .empty p {
    margin: 0;
    font-size: 0.8rem;
  }

  @media (max-width: 560px) {
    .cards {
      grid-auto-columns: 72vw;
      mask-image: none;
    }

    .play-disc,
    .enqueue {
      opacity: 1;
      transform: none;
    }
  }

  /* Touch and TV: hover-only affordances stay visible without a pointer. */
  @media (hover: none) {
    .play-disc,
    .enqueue {
      opacity: 1;
      transform: none;
    }
  }

  /* Touch devices: card controls meet the 44x44px target floor. */
  @media (pointer: coarse) {
    .play-disc,
    .enqueue {
      width: 44px;
      height: 44px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .art,
    .play-disc,
    .enqueue,
    .rail-arrow {
      transition: none;
    }

    .eq i {
      animation: none;
    }
  }
</style>
