<script lang="ts">
  export let label: string;
  export let tone:
    | 'online'
    | 'offline'
    | 'preview'
    | 'mock'
    | 'warning'
    | 'error'
    | 'neutral' = 'neutral';
  export let dot = true;
  export let title: string | undefined = undefined;
  export let material: 'none' | 'interactive' | 'solid' = 'none';
</script>

<span class="status-pill {tone} {material !== 'none' ? `material-${material}` : ''}" data-material={material} {title} role="status">
  {#if dot}<span class="dot" aria-hidden="true"></span>{/if}
  <span>{label}</span>
</span>

<style>
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 9px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    color: var(--text-muted);
    background: var(--surface);
    font-size: var(--text-micro);
    font-weight: 600;
    line-height: 1.2;
    white-space: nowrap;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 15%, transparent);
  }

  .online { color: var(--success); border-color: color-mix(in srgb, var(--success) 30%, transparent); }
  .offline { color: var(--text-muted); }
  .preview { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 30%, transparent); }
  .mock { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 30%, transparent); }
  .warning { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 35%, transparent); }
  .error { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 35%, transparent); }

  .status-pill.material-interactive {
    border-color: var(--material-edge-soft);
    background: var(--material-interactive-fill);
  }

  .status-pill.material-solid {
    border-color: var(--material-edge-soft);
    background: var(--material-solid);
  }

  .status-pill.material-interactive:hover,
  .status-pill.material-interactive:active {
    transform: none;
  }
</style>
