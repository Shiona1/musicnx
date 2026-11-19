<script lang="ts">
  export let level: 1 | 2 | 3 = 2;
  export let selected = false;
  export let interactive = false;
  export let padded = true;
  export let as: 'div' | 'section' | 'article' | 'aside' = 'div';
  /** `legacy` preserves the Stage 1 surface exactly; materials are explicit opt-in. */
  export let material:
    | 'legacy'
    | 'chrome'
    | 'overlay'
    | 'interactive'
    | 'content'
    | 'solid' = 'legacy';

  $: surfaceClass = material === 'legacy' ? `level-${level}` : `material-${material}`;
  $: selectedClass = selected ? (material === 'legacy' ? 'selected' : 'is-selected') : '';
  $: interactiveClass = interactive ? (material === 'legacy' ? 'interactive' : 'is-interactive') : '';
</script>

<svelte:element
  this={as}
  class="nx-surface {surfaceClass} {selectedClass} {interactiveClass}"
  class:padded
  data-material={material}
>
  <slot />
</svelte:element>

<style>
  .nx-surface {
    position: relative;
    overflow: hidden;
  }

  /* Legacy Stage 1 surface: unchanged appearance. */
  .level-1,
  .level-2,
  .level-3 {
    border: 1px solid var(--stroke);
    border-radius: var(--radius-card);
    background: var(--glass-2);
    box-shadow: 0 24px 60px -38px rgba(0, 0, 0, 0.9);
    backdrop-filter: blur(var(--blur));
  }

  .padded { padding: clamp(16px, 2vw, 24px); }
  .level-1 { background: var(--glass-1); border-radius: var(--radius-panel); }
  .level-3 { background: var(--glass-3); border-color: var(--stroke-strong); box-shadow: 0 30px 80px -30px #000; }

  .interactive:hover {
    border-color: var(--stroke-strong);
  }

  .selected {
    border-color: color-mix(in srgb, var(--accent) 58%, transparent);
    box-shadow:
      0 0 0 1px color-mix(in srgb, var(--accent) 20%, transparent),
      0 26px 64px -36px color-mix(in srgb, var(--accent) 28%, #000);
  }

  :global(body.reduce-effects) .level-1,
  :global(body.reduce-effects) .level-2,
  :global(body.reduce-effects) .level-3 {
    backdrop-filter: none;
    background: var(--bg-elevated);
  }
</style>
