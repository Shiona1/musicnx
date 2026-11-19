<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import CoverArt from './CoverArt.svelte';
  import Icon from './Icon.svelte';
  import { artistNames, formatTime } from '../lib/art';
  import { activeLineIndex, hasLyricsContent } from '../lib/lyrics';
  import { loadLyrics, lyricsEntry } from '../lib/lyricsStores';
  import type { Track } from '../lib/types';

  export let track: Track | null = null;
  export let position = 0;
  export let onSeek: (positionMs: number) => void;
  export let onClose: () => void;

  let scroller: HTMLOListElement | null = null;
  let autoScrollPausedUntil = 0;
  let lastActive = -1;
  let frame = 0;

  $: entry = $lyricsEntry;
  $: data = entry.trackId === track?.id ? entry.data : null;
  $: lines = data?.lines ?? [];
  $: offset = data?.offset_ms ?? 0;
  $: active = activeLineIndex(lines, position, offset);
  $: upcoming = active >= 0 && active < lines.length - 1 ? lines[active + 1] : null;

  $: if (track?.id) {
    void loadLyrics(track.id);
  }

  $: if (active !== lastActive) {
    lastActive = active;
    void centerActive(active);
  }

  onMount(() => {
    // Scroll lock is owned by App.svelte (a single writer). Restoring a saved
    // `body.style.overflow` here used to race with the App and leave the page
    // permanently unscrollable after closing karaoke.
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  });

  onDestroy(() => cancelAnimationFrame(frame));

  async function centerActive(index: number): Promise<void> {
    if (index < 0 || !scroller) return;
    if (Date.now() < autoScrollPausedUntil) return;
    await tick();
    const node = scroller.querySelector<HTMLElement>(`[data-line="${index}"]`);
    if (!node) return;
    scroller.scrollTo({
      top: Math.max(0, node.offsetTop - scroller.clientHeight / 2 + node.clientHeight / 2),
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    });
  }

  function manualScroll(): void {
    autoScrollPausedUntil = Date.now() + 5000;
  }

  function seekToLine(index: number): void {
    const line = lines[index];
    if (line) onSeek(Math.max(0, line.time_ms + offset));
  }
</script>

<!-- Fullscreen karaoke presentation; playback stays owned by the server. -->
<div class="karaoke" role="dialog" aria-modal="true" aria-label="Караоке">
  <div class="wash" aria-hidden="true"></div>

  <button class="collapse" type="button" aria-label="Свернуть караоке" title="Свернуть (Esc)" onclick={onClose}>
    <Icon name="chevron-right" size={20} />
  </button>

  <div class="karaoke-grid">
    <aside class="now">
      <div class="cover">
        <CoverArt url={track?.cover_url} title={track?.title ?? 'MusicNX'} alt="" size={1000} eager />
      </div>
      <h1>{track?.title ?? 'Ничего не играет'}</h1>
      <p class="artist">{track ? artistNames(track.artists) : 'Выберите трек'}</p>
      <div class="progress" aria-hidden="true">
        <span style={`width:${track?.duration_ms ? Math.min(100, (position / track.duration_ms) * 100) : 0}%`}></span>
      </div>
      <div class="times">
        <span>{formatTime(position)}</span>
        <span>{formatTime(track?.duration_ms ?? 0)}</span>
      </div>
      {#if upcoming}
        <p class="next-line" aria-hidden="true">{upcoming.text}</p>
      {/if}
    </aside>

    <section class="lyrics" aria-label="Текст песни">
      {#if entry.status === 'loading'}
        <div class="state"><span class="pulse"></span>Загружаем текст…</div>
      {:else if entry.status === 'error'}
        <div class="state">Текст недоступен: {entry.error}</div>
      {:else if !hasLyricsContent(data)}
        <div class="state">
          <strong>Текста пока нет</strong>
          <p>Сервер не нашёл LRC или plain-текст для этого трека.</p>
        </div>
      {:else if data?.format === 'plain' || lines.length === 0}
        <div class="plain">{data?.plain}</div>
      {:else}
        <ol class="lines" bind:this={scroller} onscroll={manualScroll} aria-label="Строки текста">
          {#each lines as line, index (index)}
            <li class:active={index === active} class:past={index < active} data-line={index}>
              <button type="button" onclick={() => seekToLine(index)} aria-label={`Перейти к строке ${index + 1}: ${line.text}`}>
                {line.text}
              </button>
            </li>
          {/each}
        </ol>
      {/if}
    </section>
  </div>
</div>

<style>
  .karaoke {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: grid;
    place-items: center;
    padding: clamp(18px, 4vw, 64px);
    background: #05070c;
    color: #f5f7ff;
    overflow: hidden;
  }

  .wash {
    position: absolute;
    inset: -10%;
    background:
      radial-gradient(900px 700px at 16% 20%, rgba(139, 92, 246, 0.26), transparent 62%),
      radial-gradient(900px 700px at 86% 80%, rgba(34, 211, 238, 0.16), transparent 64%),
      #05070c;
    filter: blur(60px);
    opacity: 0.9;
  }

  .karaoke-grid {
    position: relative;
    z-index: 1;
    width: min(1500px, 100%);
    height: min(900px, calc(100vh - 48px));
    display: grid;
    grid-template-columns: minmax(280px, 36%) minmax(0, 1fr);
    gap: clamp(28px, 5vw, 90px);
    align-items: center;
  }

  .now {
    align-self: center;
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }

  .cover {
    aspect-ratio: 1;
    width: min(420px, 100%);
    border-radius: 26px;
    overflow: hidden;
    box-shadow: 0 44px 90px -30px rgba(0, 0, 0, 0.9);
    border: 1px solid rgba(255, 255, 255, 0.12);
  }

  h1 {
    margin: 8px 0 0;
    font-size: clamp(1.6rem, 3.2vw, 2.7rem);
    line-height: 1.08;
    letter-spacing: -0.03em;
  }

  .artist {
    margin: 0;
    color: rgba(238, 242, 251, 0.62);
    font-size: 1rem;
  }

  .progress {
    height: 4px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.12);
    overflow: hidden;
    margin-top: 10px;
  }

  .progress span {
    display: block;
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    transition: width 220ms linear;
  }

  .times {
    display: flex;
    justify-content: space-between;
    color: rgba(238, 242, 251, 0.5);
    font-size: 0.72rem;
  }

  .next-line {
    margin: 10px 0 0;
    color: rgba(238, 242, 251, 0.38);
    font-size: 0.9rem;
    line-height: 1.5;
  }

  .lyrics {
    height: 100%;
    min-height: 0;
    display: flex;
    align-items: center;
  }

  .lines {
    list-style: none;
    margin: 0;
    padding: 38vh 0;
    width: 100%;
    max-height: 100%;
    overflow-y: auto;
    scrollbar-width: none;
    mask-image: linear-gradient(to bottom, transparent, #000 16%, #000 84%, transparent);
  }

  .lines::-webkit-scrollbar { display: none; }

  .lines li {
    margin: 0;
    opacity: 0.26;
    transition: opacity 260ms ease, transform 260ms ease;
  }

  .lines li.past { opacity: 0.14; }
  .lines li.active { opacity: 1; transform: translateX(6px); }

  .lines button {
    display: block;
    width: 100%;
    padding: 10px 0;
    text-align: left;
    font-size: clamp(1.2rem, 2.6vw, 2.15rem);
    font-weight: 650;
    line-height: 1.28;
    letter-spacing: -0.02em;
    color: inherit;
    transition: color 200ms ease;
  }

  .lines li.active button { color: #fff; text-shadow: 0 0 34px rgba(139, 92, 246, 0.35); }
  .lines button:hover { color: #fff; }

  .plain {
    max-height: 100%;
    overflow-y: auto;
    white-space: pre-wrap;
    font-size: clamp(1rem, 1.5vw, 1.25rem);
    line-height: 1.75;
    color: rgba(238, 242, 251, 0.8);
  }

  .state {
    color: rgba(238, 242, 251, 0.65);
    font-size: 1rem;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .state strong { color: #fff; font-size: 1.3rem; }
  .state p { margin: 0; }

  .pulse {
    width: 34px;
    height: 4px;
    border-radius: 999px;
    background: var(--accent-2);
    animation: pulse 1.1s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.25; transform: scaleX(0.4); }
    50% { opacity: 1; transform: scaleX(1); }
  }

  .collapse {
    position: absolute;
    z-index: 2;
    top: 20px;
    right: 22px;
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border-radius: 50%;
    border: 1px solid rgba(255, 255, 255, 0.14);
    background: rgba(10, 14, 24, 0.55);
    color: #fff;
  }

  @media (max-width: 860px) {
    .karaoke-grid {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
      gap: 18px;
      height: calc(100vh - 32px);
      align-items: start;
    }

    .now {
      flex-direction: row;
      align-items: center;
      gap: 14px;
    }

    .cover { width: 110px; border-radius: 16px; }
    h1 { font-size: 1.15rem; margin: 0; }
    .artist { font-size: 0.84rem; }
    .progress, .times, .next-line { display: none; }
    .lyrics { min-height: 0; }
    .lines { padding: 22vh 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .wash { filter: none; opacity: 0.5; }
    .lines li, .lines button, .progress span { transition: none; }
    .pulse { animation: none; }
  }

  :global(body.reduce-effects) .wash { filter: none; opacity: 0.55; }
</style>
