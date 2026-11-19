<script lang="ts">
  import Icon from '../Icon.svelte';
  import type { PrimarySection, SystemPage } from '../../lib/routes';

  export let section: PrimarySection = 'now';
  export let systemPage: SystemPage = 'sync';
  export let onnavigate: (section: PrimarySection) => void = () => {};
  export let onsystem: (page: SystemPage) => void = () => {};

  // Phone navigation lives in the thumb zone, Spotify-style: the player dock
  // stays directly above this bar, this bar is the bottom-most element.
  const items: { id: PrimarySection; label: string; icon: string }[] = [
    { id: 'now', label: 'Сейчас', icon: 'play' },
    { id: 'library', label: 'Медиатека', icon: 'music' },
    { id: 'yandex', label: 'Яндекс', icon: 'search' },
    { id: 'downloads', label: 'Загрузки', icon: 'download' },
    { id: 'system', label: 'Система', icon: 'cpu' },
  ];

  function go(id: PrimarySection): void {
    // «Система» reuses the last system page, the sub-row in the top bar
    // switches between them.
    if (id === 'system') onsystem(systemPage);
    else onnavigate(id);
  }
</script>

<nav class="bottom-nav material-chrome" aria-label="Основные разделы">
  {#each items as item (item.id)}
    <button
      type="button"
      class:active={section === item.id}
      aria-current={section === item.id ? 'page' : undefined}
      onclick={() => go(item.id)}
    >
      <Icon name={item.icon} size={19} />
      <span>{item.label}</span>
    </button>
  {/each}
</nav>

<style>
  .bottom-nav {
    display: none;
  }

  @media (max-width: 900px) {
    .bottom-nav {
      display: flex;
      align-items: stretch;
      gap: 2px;
      width: 100%;
      padding: 4px 10px calc(6px + env(safe-area-inset-bottom, 0px));
      border: 0;
      border-top: 1px solid var(--material-edge);
      border-radius: 0;
    }

    .bottom-nav button {
      display: flex;
      flex: 1;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 3px;
      min-height: 50px;
      border-radius: var(--radius-sm);
      color: var(--text-muted);
      font-size: var(--text-micro);
      font-weight: 600;
    }

    .bottom-nav button.active {
      color: var(--cover-accent);
    }
  }
</style>
