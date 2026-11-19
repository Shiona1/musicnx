<script lang="ts">
  export let label: string;
  export let title: string | undefined = undefined;
  export let disabled = false;
  export let active = false;
  export let size: 'sm' | 'md' = 'md';
  export let expanded: boolean | undefined = undefined;
  export let controls: string | undefined = undefined;
  export let id: string | undefined = undefined;
  export let material: 'none' | 'interactive' | 'solid' = 'none';
  export let onclick: ((event: MouseEvent) => void) | undefined = undefined;
</script>

<button
  class="nx-icon-button {size} {material !== 'none' ? `material-${material}` : ''}"
  class:active
  type="button"
  {id}
  data-material={material}
  aria-label={label}
  title={title ?? label}
  aria-pressed={active ? 'true' : undefined}
  aria-expanded={expanded}
  aria-controls={controls}
  {disabled}
  {onclick}
>
  <slot />
</button>

<style>
  .nx-icon-button {
    display: inline-grid;
    place-items: center;
    border: 1px solid transparent;
    border-radius: var(--radius-pill);
    color: var(--text-soft);
    background: transparent;
    transition:
      color var(--motion-fast) ease,
      background var(--motion-fast) ease,
      border-color var(--motion-fast) ease;
  }

  .sm { width: 32px; height: 32px; }
  .md { width: 40px; height: 40px; }

  .nx-icon-button:hover:not(:disabled),
  .nx-icon-button.active {
    color: var(--text);
    background: var(--surface-hover);
    border-color: var(--stroke);
  }

  .nx-icon-button:disabled { opacity: 0.35; }

  .nx-icon-button.material-interactive {
    border-radius: var(--radius-pill);
    border-color: var(--material-edge-soft);
    background: var(--material-interactive-fill);
    box-shadow: var(--material-inner-top);
  }

  .nx-icon-button.material-interactive:hover:not(:disabled),
  .nx-icon-button.material-interactive.active {
    border-color: var(--material-edge);
    background: var(--material-interactive-hover);
  }

  .nx-icon-button.material-solid {
    border-radius: var(--radius-pill);
    border-color: var(--material-edge-soft);
    background: var(--material-solid);
  }

  .nx-icon-button.material-solid:hover:not(:disabled),
  .nx-icon-button.material-solid.active {
    border-color: var(--material-edge);
    background: var(--material-solid);
  }

  /* Touch devices: every icon control meets the 44x44px target floor. */
  @media (pointer: coarse) {
    .sm,
    .md {
      width: 44px;
      height: 44px;
    }
  }
</style>
