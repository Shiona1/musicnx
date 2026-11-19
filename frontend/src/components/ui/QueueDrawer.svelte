<script lang="ts">
  import { fade, fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import CoverArt from '../CoverArt.svelte';
  import Icon from '../Icon.svelte';
  import IconButton from './IconButton.svelte';
  import EmptyState from './EmptyState.svelte';
  import { artistNames, formatDuration } from '../../lib/art';
  import type { QueueState } from '../../lib/types';

  export let open = false;
  export let queue: QueueState | null = null;
  export let busy = false;
  export let onClose: () => void = () => {};
  export let onRemove: (itemId: number) => void = () => {};
  export let onPlayTrack: (trackId: number) => void = () => {};
  export let onMoveUp: (itemId: number) => void = () => {};
  export let onMoveDown: (itemId: number) => void = () => {};
  export let onClear: () => void = () => {};

  $: items = queue?.items ?? [];
  $: total = queue?.total ?? 0;
  $: currentId = queue?.current_item_id ?? null;
  $: currentIndex = queue?.current_index ?? null;
  $: upcoming =
    currentIndex === null ? items.length : items.length - currentIndex - 1;
  $: upcomingMs =
    currentIndex === null
      ? 0
      : items
          .slice(currentIndex + 1)
          .reduce((acc, item) => acc + (item.track.duration_ms ?? 0), 0);

  let confirmClear = false;

  function tracksWord(count: number): string {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return 'трек';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return 'трека';
    return 'треков';
  }

  let previousFocus: HTMLElement | null = null;
  let drawerEl: HTMLElement | null = null;
  let lastOpen = false;
  let focusSeq = 0;

  function closeDrawer(): void {
    onClose();
  }

  function handleKey(event: KeyboardEvent): void {
    if (open && event.key === 'Escape') closeDrawer();
  }

  $: if (open !== lastOpen) {
    const seq = ++focusSeq;
    if (open) {
      previousFocus =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      queueMicrotask(() => drawerEl?.focus());
    } else {
      const target = previousFocus;
      previousFocus = null;
      setTimeout(
        () => {
          if (seq === focusSeq) target?.focus();
        },
        reducedMotion() ? 0 : 230,
      );
    }
    lastOpen = open;
  }

  function trapFocus(event: KeyboardEvent): void {
    if (event.key !== 'Tab') return;
    const root = event.currentTarget as HTMLElement;
    const focusable = Array.from(
      root.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function reducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function isMobile(): boolean {
    return typeof window !== 'undefined' && window.matchMedia('(max-width: 760px)').matches;
  }

  function drawerIn(node: HTMLElement) {
    const duration = reducedMotion() ? 0 : 240;
    return isMobile()
      ? fly(node, { y: 18, duration, easing: cubicOut })
      : fly(node, { x: 18, duration, easing: cubicOut });
  }

  function drawerOut(node: HTMLElement) {
    const duration = reducedMotion() ? 0 : 200;
    return isMobile()
      ? fly(node, { y: 12, duration, easing: cubicOut })
      : fly(node, { x: 12, duration, easing: cubicOut });
  }

  function scrimIn(node: HTMLElement) {
    return fade(node, { duration: reducedMotion() ? 0 : 180 });
  }

  function scrimOut(node: HTMLElement) {
    return fade(node, { duration: reducedMotion() ? 0 : 150 });
  }
</script>

<svelte:window onkeydown={handleKey} />

{#if open}
  <div
    class="queue-layer material-scrim"
    role="presentation"
    in:scrimIn
    out:scrimOut
    onclick={(event) => event.target === event.currentTarget && closeDrawer()}
  >
    <div
      class="queue-drawer material-overlay"
      id="queue-drawer"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      bind:this={drawerEl}
      onkeydown={trapFocus}
      in:drawerIn
      out:drawerOut
      aria-labelledby="queue-drawer-title"
    >
      <header class="queue-head">
        <div>
          <span class="eyebrow">Сейчас играет</span>
          <h2 id="queue-drawer-title">Очередь</h2>
          {#if upcoming > 0}
            <p class="up-next">
              Далее: {upcoming} {tracksWord(upcoming)} · {formatDuration(upcomingMs)}
            </p>
          {/if}
        </div>
        <div class="head-side">
          <span class="count">{total}</span>
          {#if confirmClear}
            <span class="confirm" role="group" aria-label="Подтверждение очистки очереди">
              Очистить?
              <button type="button" class="head-action danger" onclick={() => { confirmClear = false; onClear(); }}>
                Да, очистить
              </button>
              <button type="button" class="head-action" onclick={() => (confirmClear = false)}>
                Нет
              </button>
            </span>
          {:else if upcoming > 0}
            <button
              type="button"
              class="head-action"
              title="Убрать будущие треки — текущий продолжит играть"
              onclick={() => (confirmClear = true)}
            >
              Очистить
            </button>
          {/if}
          <IconButton label="Закрыть очередь" onclick={closeDrawer}><Icon name="close" size={17} /></IconButton>
        </div>
      </header>

      {#if items.length === 0}
        <EmptyState
          title="Очередь пуста"
          description="Добавьте трек с полки или из Яндекс Музыки — он появится здесь."
        />
      {:else}
        <ol class="queue-items">
          {#each items as item, index (item.id)}
            {#if item.id === currentId && index < items.length - 1}
              <li class="up-divider" aria-hidden="true"><span>Далее</span></li>
            {/if}
            <li class:current={item.id === currentId}>
              <button class="item-main" type="button" aria-label={`Играть «${item.track.title}»`} onclick={() => onPlayTrack(item.track.id)}>
                <span class="item-cover">
                  <CoverArt url={item.track.cover_url} title={item.track.title} size={96} />
                  {#if item.id === currentId}<span class="item-live" aria-label="Сейчас играет"><i></i><i></i><i></i></span>{/if}
                </span>
                <span class="item-copy">
                  <span class="item-title">{item.track.title}</span>
                  <span class="item-artist">{artistNames(item.track.artists)}</span>
                </span>
                <span class="item-index">{index + 1}</span>
                <span class="item-duration">{formatDuration(item.track.duration_ms)}</span>
              </button>
              <span class="item-tools">
                {#if item.id !== currentId}
                  <IconButton
                    label={`Поднять «${item.track.title}» выше`}
                    disabled={index === 0 || busy}
                    onclick={() => onMoveUp(item.id)}
                  >
                    <Icon name="chevron-up" size={14} />
                  </IconButton>
                  <IconButton
                    label={`Опустить «${item.track.title}» ниже`}
                    disabled={index === items.length - 1 || busy}
                    onclick={() => onMoveDown(item.id)}
                  >
                    <Icon name="chevron-down" size={14} />
                  </IconButton>
                {/if}
                <IconButton label={`Убрать «${item.track.title}» из очереди`} disabled={busy} onclick={() => onRemove(item.id)}>
                  <Icon name="close" size={14} />
                </IconButton>
              </span>
            </li>
          {/each}
        </ol>
      {/if}
    </div>
  </div>
{/if}

<style>
  .queue-layer {
    position: fixed;
    inset: 0;
    z-index: 40;
    display: flex;
    justify-content: flex-end;
  }

  .queue-drawer {
    width: min(390px, 100%);
    height: calc(100% - var(--dock-height) - var(--bnav-height));
    margin-top: 0;
    padding: 18px;
    border-radius: var(--radius-panel) 0 0 var(--radius-panel);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .queue-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .eyebrow {
    color: var(--cover-accent);
    font-size: var(--text-micro);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  h2 { margin: 3px 0 0; font-size: var(--text-section); }
  .head-side { display: flex; align-items: center; gap: 6px; }
  .count {
    min-width: 22px;
    padding: 2px 6px;
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-muted);
    font-size: var(--text-micro);
    text-align: center;
  }

  .up-next {
    margin: 2px 0 0;
    color: var(--text-muted);
    font-size: var(--text-meta);
  }

  .head-action {
    padding: 6px 12px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-soft);
    font-size: var(--text-meta);
    font-weight: 600;
  }

  .head-action:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .head-action.danger {
    color: #fecdd3;
    border-color: color-mix(in srgb, var(--danger) 30%, transparent);
  }

  .confirm {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #fecdd3;
    font-size: var(--text-meta);
    font-weight: 600;
  }

  .up-divider {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 8px 2px 2px;
    color: var(--text-muted);
    font-size: var(--text-micro);
  }

  .up-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--stroke);
  }

  .item-tools {
    display: flex;
    gap: 2px;
  }

  @media (pointer: coarse) {
    .head-action {
      min-height: 44px;
    }
  }

  .queue-items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; overflow-y: auto; overscroll-behavior: contain; }
  .queue-items li {
    display: flex;
    align-items: center;
    gap: 4px;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    padding: 4px;
  }
  .queue-items li:hover { background: var(--surface); }
  .queue-items li.current { border-color: color-mix(in srgb, var(--cover-accent) 42%, transparent); background: var(--cover-accent-soft); }

  .item-main {
    display: grid;
    grid-template-columns: 42px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 9px;
    flex: 1;
    min-width: 0;
    color: var(--text);
    text-align: left;
  }

  .item-cover { position: relative; width: 42px; height: 42px; border-radius: 10px; overflow: hidden; border: 1px solid var(--stroke); }
  .item-copy { display: flex; flex-direction: column; min-width: 0; gap: 1px; }
  .item-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: var(--text-meta); font-weight: 600; }
  .item-artist { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-muted); font-size: var(--text-micro); }
  .item-index { color: var(--text-muted); font-size: var(--text-micro); font-variant-numeric: tabular-nums; }
  .item-duration { color: var(--text-muted); font-size: var(--text-micro); font-variant-numeric: tabular-nums; }

  .item-live { position: absolute; inset: auto 3px 3px auto; display: inline-flex; align-items: flex-end; gap: 1px; height: 8px; }
  .item-live i { width: 2px; height: 100%; background: var(--cover-accent); animation: eq 0.9s ease-in-out infinite; }
  .item-live i:nth-child(2) { animation-delay: .15s; }
  .item-live i:nth-child(3) { animation-delay: .3s; }

  @keyframes eq { 0%,100% { transform: scaleY(.35); } 50% { transform: scaleY(1); } }

  @media (max-width: 760px) {
    .queue-layer { align-items: flex-end; }
    .queue-drawer {
      width: 100%;
      height: auto;
      max-height: min(72vh, calc(100% - var(--dock-height) - var(--bnav-height)));
      border-radius: var(--radius-panel) var(--radius-panel) 0 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .item-live i { animation: none; }
  }
</style>
