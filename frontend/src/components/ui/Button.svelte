<script lang="ts">
  export let variant: 'primary' | 'secondary' | 'ghost' | 'danger' = 'secondary';
  export let size: 'sm' | 'md' | 'lg' = 'md';
  export let type: 'button' | 'submit' = 'button';
  export let disabled = false;
  export let full = false;
  export let ariaLabel: string | undefined = undefined;
  export let title: string | undefined = undefined;
  export let material: 'none' | 'interactive' | 'solid' | 'content' = 'none';
  export let onclick: ((event: MouseEvent) => void) | undefined = undefined;
</script>

<button
  class="nx-button {variant} {size} {material !== 'none' ? `material-${material}` : ''}"
  class:full
  data-material={material}
  {type}
  {disabled}
  aria-label={ariaLabel}
  {title}
  {onclick}
>
  <slot />
</button>

<style>
  .nx-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    padding: 0 16px;
    color: var(--text);
    background: var(--surface);
    font-size: var(--text-body);
    font-weight: 600;
    white-space: nowrap;
    transition:
      transform var(--motion-fast) var(--ease-out),
      background var(--motion-fast) ease,
      border-color var(--motion-fast) ease;
  }

  .nx-button:hover:not(:disabled) {
    background: var(--surface-hover);
    border-color: var(--stroke-strong);
    transform: translateY(-1px);
  }

  .nx-button:active:not(:disabled) {
    transform: translateY(0) scale(0.985);
  }

  .nx-button:disabled {
    opacity: 0.42;
  }

  .sm { height: 32px; padding-inline: 12px; font-size: var(--text-meta); }
  .md { height: 40px; }
  .lg { height: 48px; padding-inline: 22px; }

  .primary {
    border-color: transparent;
    background: var(--accent);
    color: #080b12;
    box-shadow: 0 12px 30px -16px color-mix(in srgb, var(--accent) 75%, transparent);
  }

  .primary:hover:not(:disabled) {
    background: color-mix(in srgb, var(--accent) 88%, white);
  }

  .ghost {
    border-color: transparent;
    background: transparent;
    color: var(--text-soft);
  }

  .danger {
    color: var(--danger);
    border-color: color-mix(in srgb, var(--danger) 42%, transparent);
    background: color-mix(in srgb, var(--danger) 10%, transparent);
  }

  .full { width: 100%; }

  /* New materials are explicit; default buttons keep their Stage 1 look. */
  .nx-button.material-interactive,
  .nx-button.material-content,
  .nx-button.material-solid {
    border-radius: var(--radius-pill);
  }

  .nx-button.material-interactive {
    border-color: var(--material-edge-soft);
    background: var(--material-interactive-fill);
    box-shadow: var(--material-inner-top);
  }

  .nx-button.material-interactive:hover:not(:disabled) {
    border-color: var(--material-edge);
    background: var(--material-interactive-hover);
  }

  .nx-button.material-content {
    border-color: var(--material-edge-soft);
    background: var(--material-content-fill);
  }

  .nx-button.material-solid {
    border-color: var(--material-edge-soft);
    background: var(--material-solid);
  }

  .nx-button.material-solid:hover:not(:disabled) {
    border-color: var(--material-edge);
    background: var(--material-solid);
  }

  .nx-button.material-content:hover:not(:disabled) {
    border-color: var(--material-edge);
    background: var(--material-content-fill);
  }

  .nx-button.material-content {
    box-shadow: none;
  }

  @media (prefers-reduced-motion: reduce) {
    .nx-button { transition: none; }
    .nx-button:hover:not(:disabled) { transform: none; }
  }

  /* Touch devices: the small size still meets the 44px target floor. */
  @media (pointer: coarse) {
    .sm { height: 44px; }
  }
</style>
