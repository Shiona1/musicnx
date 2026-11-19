<script lang="ts">
  import StatusPill from './StatusPill.svelte';
  import QuickControls from './QuickControls.svelte';
  import type { ConnectionState } from '../../lib/types';
  import type { PrimarySection, SystemPage } from '../../lib/routes';

  export let section: PrimarySection = 'now';
  export let systemPage: SystemPage = 'sync';
  export let mode = 'mock';
  export let connection: ConnectionState = 'connecting';
  export let onnavigate: (section: PrimarySection) => void = () => {};
  export let onsystem: (page: SystemPage) => void = () => {};
  export let onToast: (message: string) => void = () => {};

  const primary: { id: PrimarySection; label: string }[] = [
    { id: 'now', label: 'Сейчас' },
    { id: 'library', label: 'Медиатека' },
    { id: 'yandex', label: 'Яндекс' },
    { id: 'downloads', label: 'Загрузки' },
    { id: 'system', label: 'Система' },
  ];

  const system: { id: SystemPage; label: string }[] = [
    { id: 'state', label: 'Состояние' },
    { id: 'sync', label: 'Синхронизация' },
    { id: 'zones', label: 'Зоны' },
    { id: 'network', label: 'Сеть' },
    { id: 'settings', label: 'Настройки' },
  ];

  const connectionLabels: Record<ConnectionState, string> = {
    connecting: 'Подключение',
    online: 'В сети',
    reconnecting: 'Переподключение',
    offline: 'Нет связи',
  };

  function go(id: PrimarySection): void {
    onnavigate(id);
  }

  function goSystem(id: SystemPage): void {
    onsystem(id);
  }

  let connectionTone: 'online' | 'warning' | 'error' = 'warning';
  $: connectionTone =
    connection === 'online' ? 'online' : connection === 'offline' ? 'error' : 'warning';
</script>

<div class="system-bar-shell">
  <header class="system-bar material-chrome">
  <div class="bar-inner">
    <button class="brand" type="button" aria-label="MusicNX — на главную" onclick={() => go('now')}>
      <span class="mark" aria-hidden="true">
        <svg viewBox="0 0 64 64" width="34" height="34">
          <rect width="64" height="64" rx="18" fill="rgba(255,255,255,.06)" />
          <circle cx="32" cy="32" r="17" fill="none" stroke="currentColor" stroke-width="3" />
          <path d="M38 20v18.5a6.5 6.5 0 1 1-4-6V24l10-2.5V18z" fill="currentColor" />
        </svg>
      </span>
      <span class="brand-text">
        <span class="brand-name">MusicNX</span>
        <span class="brand-tag">music center</span>
      </span>
    </button>

    <nav class="primary-nav" aria-label="Основные разделы">
      {#each primary as item (item.id)}
        <button
          type="button"
          class:active={section === item.id}
          aria-current={section === item.id ? 'page' : undefined}
          onclick={() => go(item.id)}
        >
          {item.label}
        </button>
      {/each}
    </nav>

    <div class="status-cluster">
      <QuickControls
        onOpenNetwork={() => onsystem('network')}
        onOpenZones={() => onsystem('zones')}
        {onToast}
      />
      <StatusPill
        label={mode === 'mock' ? 'Mock' : 'Real'}
        tone={mode === 'mock' ? 'mock' : 'online'}
        title={mode === 'mock' ? 'Виртуальное аудио' : 'Реальный mpv'}
      />
      <StatusPill
        label={connectionLabels[connection]}
        tone={connectionTone}
        title="Состояние WebSocket"
      />
    </div>
  </div>

  {#if section === 'system'}
    <nav class="system-nav" aria-label="Системные разделы">
      {#each system as item (item.id)}
        <button
          type="button"
          class:active={systemPage === item.id}
          aria-current={systemPage === item.id ? 'page' : undefined}
          onclick={() => goSystem(item.id)}
        >
          {item.label}
        </button>
      {/each}
    </nav>
  {/if}

</header>

</div>

<style>
  .system-bar-shell {
    position: relative;
  }

  .system-bar {
    position: relative;
  }

  .system-bar.material-chrome {
    border: 0;
    border-bottom: 1px solid var(--material-edge);
    border-radius: 0;
    overflow: visible;
    background-image: radial-gradient(
      120% 160% at 18% 0%,
      color-mix(in srgb, var(--cover-accent) 14%, transparent),
      transparent 62%
    );
    box-shadow:
      0 18px 44px -36px rgba(0, 0, 0, 0.92),
      var(--material-inner-top);
  }

  .bar-inner {
    display: flex;
    align-items: center;
    gap: 18px;
    width: 100%;
    max-width: var(--page-max);
    margin: 0 auto;
    padding: 10px var(--page-gutter);
  }

  .brand {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    color: var(--text);
    text-align: left;
  }

  .mark { color: var(--accent-2); flex: none; }

  .brand-text {
    display: flex;
    flex-direction: column;
    line-height: 1.06;
    min-width: 0;
  }

  .brand-name { font-size: 1rem; font-weight: 700; letter-spacing: 0.01em; }
  .brand-tag {
    color: var(--text-muted);
    font-size: 0.69rem;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .primary-nav {
    display: flex;
    align-items: center;
    gap: 2px;
    margin-inline: auto;
    padding: 3px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: rgba(255, 255, 255, 0.035);
  }

  .primary-nav button,
  .system-nav button {
    border-radius: var(--radius-pill);
    padding: 7px 14px;
    color: var(--text-muted);
    font-size: var(--text-meta);
    font-weight: 600;
    white-space: nowrap;
    transition:
      color var(--motion-fast) ease,
      background var(--motion-fast) ease;
  }

  .primary-nav button:hover,
  .system-nav button:hover { color: var(--text); }

  .primary-nav button.active {
    color: #070b13;
    background: var(--cover-accent);
  }

  .status-cluster {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: none;
  }

  .system-nav {
    display: flex;
    justify-content: center;
    gap: 4px;
    padding: 0 var(--page-gutter) 8px;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
    padding-top: 6px;
  }

  .system-nav button.active {
    color: var(--text);
    background: color-mix(in srgb, var(--cover-accent) 18%, transparent);
  }

  @media (max-width: 1100px) {
    .status-cluster :global(.status-pill) { display: none; }
  }

  @media (max-width: 900px) {
    /* Phone navigation lives in the thumb zone (BottomNav below the dock);
       the top bar keeps brand, quick controls and the system sub-row. */
    .primary-nav { display: none; }
    .system-nav {
      justify-content: flex-start;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .system-nav button { padding: 7px 12px; }
    .bar-inner { justify-content: space-between; }
    .status-cluster { margin-left: auto; }
  }

  @media (max-width: 560px) {
    .bar-inner { gap: 10px; padding-block: 9px; }
    .brand-tag { display: none; }
    .brand-name { font-size: 0.94rem; }
  }
</style>
