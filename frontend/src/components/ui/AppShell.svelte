<script lang="ts">
  export let ambientStyle = '';
  export let hasArt = false;
</script>

<div class="nx-app">
  <div class="scene" aria-hidden="true">
    <div class="ambient" class:has-art={hasArt} style={ambientStyle}></div>
    <div class="wash"></div>
    <div class="grain"></div>
  </div>

  <div class="bar-layer"><slot name="bar" /></div>
  <div class="main-layer"><slot name="main" /></div>
  <div class="dock-layer"><slot name="dock" /></div>
  <slot name="queue" />
</div>

<style>
  .nx-app {
    position: relative;
    min-height: 100vh;
    min-height: 100dvh;
    isolation: isolate;
  }

  .scene {
    position: fixed;
    inset: 0;
    z-index: -1;
    overflow: hidden;
    background: var(--bg-abyss);
  }

  .ambient {
    position: absolute;
    inset: -18%;
    background-position: center;
    background-size: cover;
    filter: blur(34px) saturate(1.22) brightness(0.5);
    opacity: 0;
    transform: scale(1.08);
    transition: opacity 700ms ease;
  }

  .ambient.has-art { opacity: 0.66; }

  .wash {
    position: absolute;
    inset: 0;
    background:
      linear-gradient(180deg, rgba(5, 7, 12, 0.28), rgba(5, 7, 12, 0.78) 72%, var(--bg-abyss)),
      radial-gradient(900px 600px at 78% 0%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 70%);
  }

  .grain {
    position: absolute;
    inset: 0;
    opacity: 0.16;
    background-image: radial-gradient(rgba(255, 255, 255, 0.16) 0.5px, transparent 0.5px);
    background-size: 4px 4px;
    mix-blend-mode: soft-light;
  }

  .bar-layer {
    position: sticky;
    top: 0;
    z-index: 20;
  }

  .main-layer {
    position: relative;
    z-index: 1;
  }

  .dock-layer {
    position: fixed;
    z-index: 30;
    left: 0;
    right: 0;
    bottom: 0;
  }

  :global(body.reduce-effects) .ambient {
    filter: none;
    opacity: 0.28;
  }

  :global(body.reduce-effects) .grain {
    display: none;
  }

  @media (prefers-reduced-transparency: reduce) {
    .ambient { filter: none; opacity: 0.3; }
    .grain { display: none; }
  }

  @media (prefers-reduced-motion: reduce) {
    .ambient { transition: none; }
  }
</style>
