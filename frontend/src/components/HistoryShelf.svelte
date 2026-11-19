<script lang="ts">
  import { onMount } from 'svelte';
  import CoverArt from './CoverArt.svelte';
  import Icon from './Icon.svelte';
  import { api } from '../lib/api';
  import type { HistoryEntry } from '../lib/types';

  export let currentTrackId: number | null = null;
  export let busy = false;
  export let onPlay: (trackId: number) => void;
  export let onEnqueue: (trackId: number) => void;
  export let onToast: ((message: string) => void) | null = null;

  let items: HistoryEntry[] = [];
  let loading = true;
  let error = '';
  let clearing = false;
  let lastLoadTrack: number | null = null;

  onMount(() => {
    void load();
  });

  // A new playback is the only event that changes history; refresh when the
  // current track changes, but do not poll.
  $: if (currentTrackId !== null && currentTrackId !== lastLoadTrack) {
    lastLoadTrack = currentTrackId;
    void load();
  }

  async function load(): Promise<void> {
    loading = true;
    error = '';
    try {
      const page = await api.history(12);
      items = page.items;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'Не удалось загрузить историю.';
    } finally {
      loading = false;
    }
  }

  async function clear(): Promise<void> {
    if (clearing) return;
    if (typeof window !== 'undefined' && !window.confirm('Очистить историю прослушивания?')) {
      return;
    }
    clearing = true;
    try {
      const result = await api.clearHistory();
      items = [];
      onToast?.(`История очищена: ${result.deleted}`);
    } catch (cause) {
      onToast?.(cause instanceof Error ? cause.message : 'Не удалось очистить историю.');
    } finally {
      clearing = false;
    }
  }

  function when(value: string): string {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function artistNames(entry: HistoryEntry): string {
    return entry.track.artists.map((artist) => artist.name).join(', ');
  }
</script>

{#if loading && items.length === 0}
  <section class="history" aria-label="История прослушивания">
    <div class="head"><span class="eyebrow">Недавнее</span><h2>История</h2></div>
    <div class="skeleton"></div>
  </section>
{:else if error}
  <section class="history" aria-label="История прослушивания">
    <div class="head"><span class="eyebrow">Недавнее</span><h2>История</h2></div>
    <p class="error">{error}</p>
  </section>
{:else if items.length > 0}
  <section class="history" aria-label="История прослушивания">
    <div class="head">
      <div>
        <span class="eyebrow">Недавнее</span>
        <h2>История</h2>
      </div>
      <button type="button" class="ghost small" disabled={clearing} onclick={clear}>
        {clearing ? 'Очистка…' : 'Очистить'}
      </button>
    </div>
    <ol class="list">
      {#each items as entry (entry.id)}
        <li class="row" class:current={entry.track.id === currentTrackId}>
          <span class="cover">
            <CoverArt url={entry.track.cover_url} title={entry.track.title} size={64} />
          </span>
          <span class="copy">
            <strong title={entry.track.title}>{entry.track.title}</strong>
            <span>{artistNames(entry)} · {when(entry.played_at)}</span>
          </span>
          <span class="actions">
            <button
              type="button"
              class="icon"
              title="Играть"
              aria-label={`Играть «${entry.track.title}»`}
              disabled={busy}
              onclick={() => onPlay(entry.track.id)}
            >
              <Icon name="play" size={14} />
            </button>
            <button
              type="button"
              class="icon"
              title="В очередь"
              aria-label={`Добавить «${entry.track.title}» в очередь`}
              disabled={busy}
              onclick={() => onEnqueue(entry.track.id)}
            >
              <Icon name="plus" size={14} />
            </button>
          </span>
        </li>
      {/each}
    </ol>
  </section>
{/if}

<style>
  .history {
    margin-top: 14px;
    padding: 14px 16px;
    border-radius: 18px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.025);
  }

  .head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
  }

  .eyebrow {
    color: var(--accent-2);
    font-size: 0.64rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  h2 {
    margin: 2px 0 0;
    font-size: 0.95rem;
  }

  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    padding: 5px 6px;
    border-radius: 10px;
  }

  .row:hover,
  .row.current {
    background: var(--surface-hover);
  }

  .cover {
    flex: none;
    width: 34px;
    height: 34px;
    border-radius: 7px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.04);
  }

  .copy {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .copy strong {
    color: var(--text);
    font-size: 0.76rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .copy span {
    color: var(--muted);
    font-size: 0.66rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .actions {
    margin-left: auto;
    display: flex;
    gap: 4px;
  }

  .icon {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
  }

  .icon:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  /* Touch devices: shelf controls meet the 44x44px target floor. */
  @media (pointer: coarse) {
    .icon {
      width: 44px;
      height: 44px;
    }
  }

  .ghost.small {
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.68rem;
  }

  .skeleton {
    height: 46px;
    border-radius: 10px;
    background: linear-gradient(100deg, rgba(255, 255, 255, 0.04) 25%, rgba(255, 255, 255, 0.09) 45%, rgba(255, 255, 255, 0.04) 65%);
    background-size: 220% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .error {
    margin: 0;
    color: #fecdd3;
    font-size: 0.74rem;
  }

  @keyframes shimmer {
    0% { background-position: 120% 0; }
    100% { background-position: -40% 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton { animation: none; }
  }
</style>
