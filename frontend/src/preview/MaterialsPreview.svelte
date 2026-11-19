<script lang="ts">
  import { onMount } from 'svelte';
  import Surface from '../components/ui/Surface.svelte';
  import Button from '../components/ui/Button.svelte';
  import IconButton from '../components/ui/IconButton.svelte';
  import StatusPill from '../components/ui/StatusPill.svelte';
  import Icon from '../components/Icon.svelte';

  type Mode = 'normal' | 'lite' | 'fallback';
  type ThemeId = 'dark' | 'light' | 'cover';

  const themes = [
    { id: 'dark', label: 'DARK SCENE', sub: 'Глубокий фон + ambient wash' },
    { id: 'light', label: 'LIGHT SCENE', sub: 'Светлая сцена, тот же материал' },
    { id: 'cover', label: 'BRIGHT COVER', sub: 'Яркая обложка под стеклом' },
  ] as const;

  let mode: Mode = 'normal';
  let activeTheme: ThemeId = 'dark';

  $: visibleThemes = themes.filter((theme) => theme.id === activeTheme);

  function setMode(next: Mode): void {
    mode = next;
    if (typeof document === 'undefined') return;
    document.body.classList.toggle('reduce-effects', next === 'lite');
    document.body.classList.toggle('no-backdrop', next === 'fallback');
  }

  function setTheme(next: ThemeId): void {
    activeTheme = next;
  }

  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedMode = params.get('mode');
    if (requestedMode === 'lite' || requestedMode === 'fallback') setMode(requestedMode);
    const requestedTheme = params.get('theme');
    if (requestedTheme === 'dark' || requestedTheme === 'light' || requestedTheme === 'cover') {
      setTheme(requestedTheme);
    }
  });
</script>

<svelte:head>
  <title>MusicNX — LG1 material specimen</title>
</svelte:head>

<div class="preview">
  <header class="preview-head">
    <div class="brand">
      <span class="mark" aria-hidden="true">
        <svg viewBox="0 0 64 64" width="34" height="34">
          <rect width="64" height="64" rx="18" fill="rgba(255,255,255,.06)" />
          <circle cx="32" cy="32" r="17" fill="none" stroke="currentColor" stroke-width="3" />
          <path d="M38 20v18.5a6.5 6.5 0 1 1-4-6V24l10-2.5V18z" fill="currentColor" />
        </svg>
      </span>
      <div>
        <strong>MusicNX</strong>
        <span>LG1 · material specimen</span>
      </div>
    </div>

    <div class="toolbar">
      <div class="theme-switcher" role="group" aria-label="Сцена материалов">
        {#each themes as theme (theme.id)}
          <button
            type="button"
            class:active={activeTheme === theme.id}
            aria-pressed={activeTheme === theme.id}
            onclick={() => setTheme(theme.id)}
          >
            {theme.id === 'dark' ? 'Dark' : theme.id === 'light' ? 'Light' : 'Cover'}
          </button>
        {/each}
      </div>
      <div class="mode-switcher" role="group" aria-label="Режим отображения материалов">
        <button type="button" class:active={mode === 'normal'} aria-pressed={mode === 'normal'} onclick={() => setMode('normal')}>
          Normal
        </button>
        <button type="button" class:active={mode === 'lite'} aria-pressed={mode === 'lite'} onclick={() => setMode('lite')}>
          Lite
        </button>
        <button type="button" class:active={mode === 'fallback'} aria-pressed={mode === 'fallback'} onclick={() => setMode('fallback')}>
          No backdrop
        </button>
      </div>
    </div>
  </header>

  <div class="intro">
    <div>
      <span class="eyebrow">Material foundation · opt-in only</span>
      <h1>Liquid Glass, не «стекло на всё»</h1>
      <p>
        Chrome и overlay — единственные размытые слои. Контент и строки — solid.
        Нажмите <b>Lite</b> или <b>No backdrop</b>, чтобы увидеть fallback.
      </p>
    </div>
    <div class="budget" aria-label="Бюджет размытия">
      <span class="budget-label">visible blur budget</span>
      <strong>{mode === 'normal' ? '2' : '0'}<small>/ 3</small></strong>
      <span>{mode === 'normal' ? 'SystemBar + overlay' : 'solid fallback'}</span>
    </div>
  </div>

  <div class="mode-note" role="status">
    {#if mode === 'normal'}
      Normal: backdrop-filter активен только у chrome/overlay. 2 blurred surfaces.
    {:else if mode === 'lite'}
      Lite: body.reduce-effects — blur, specular и grain отключены; иерархия сохранена.
    {:else}
      No backdrop: эмуляция @supports not (backdrop-filter); непрозрачные поверхности.
    {/if}
  </div>

  {#each visibleThemes as theme (theme.id)}
    <section class="stage theme-{theme.id}" aria-labelledby={`theme-${theme.id}`}>
      {#if theme.id === 'cover'}
        <div class="cover-art" aria-hidden="true">
          <span class="cover-disc"></span>
          <span class="cover-stripe"></span>
        </div>
      {/if}
      <div class="stage-head">
        <div>
          <span class="stage-kicker">{theme.label}</span>
          <h2 id={`theme-${theme.id}`}>{theme.sub}</h2>
        </div>
        <span class="stage-hint">4 materials · 1 specimen board</span>
      </div>

      <div class="specimen-grid">
        <Surface material="chrome" padded>
          <div class="specimen-head">
            <span class="material-label">CHROME <b>20px</b></span>
            <StatusPill label="Real · online" tone="online" material="solid" />
          </div>
          <div class="chrome-row">
            <span class="mini-cover" aria-hidden="true"><i></i></span>
            <div class="specimen-copy">
              <strong>SystemBar / NowDock</strong>
              <span>persistent instrument · one blur layer</span>
            </div>
            <IconButton label="Очередь" material="interactive"><Icon name="queue" size={16} /></IconButton>
          </div>
          <div class="specimen-actions">
            <Button material="interactive" size="sm">Play</Button>
            <Button variant="ghost" size="sm">Queue</Button>
            <span class="micro-note">edge + inner light, no child blur</span>
          </div>
        </Surface>

        <Surface material="overlay" padded>
          <div class="specimen-head">
            <span class="material-label">OVERLAY <b>28px</b></span>
            <StatusPill label="Queue" material="interactive" />
          </div>
          <div class="overlay-title">Queue / dialog</div>
          <div class="queue-sample">
            <span class="mini-cover alt" aria-hidden="true"><i></i></span>
            <span class="specimen-copy"><strong>Track title</strong><span>Artist · 3:34</span></span>
            <span class="sample-count">1</span>
          </div>
          <div class="solid-row"><span class="row-dot"></span>Solid row inside overlay</div>
          <p class="micro-note">scrim + one overlay; content rows stay solid</p>
        </Surface>

        <Surface material="interactive" padded>
          <div class="specimen-head">
            <span class="material-label">INTERACTIVE <b>0px</b></span>
            <StatusPill label="selected" tone="online" material="solid" />
          </div>
          <div class="interactive-row">
            <Button material="interactive" size="sm">Primary action</Button>
            <IconButton label="Play" material="interactive" active><Icon name="play" size={15} /></IconButton>
            <span class="selected-chip">selected</span>
          </div>
          <div class="selected-swatch">
            <span class="swatch-thumb" aria-hidden="true"></span>
            <div class="specimen-copy"><strong>Selected card</strong><span>cover-accent rim, no blur</span></div>
            <span class="selected-dot" aria-hidden="true"></span>
          </div>
          <p class="micro-note">buttons/chips/selected cards: fill + edge + focus only</p>
        </Surface>

        <Surface material="solid" padded>
          <div class="specimen-head">
            <span class="material-label">SOLID <b>0px</b></span>
            <StatusPill label="readable" material="solid" />
          </div>
          <div class="solid-track">
            <span class="track-index">01</span>
            <span class="specimen-copy"><strong>Track title</strong><span>Artist · album</span></span>
            <span class="track-time">4:05</span>
          </div>
          <div class="solid-track muted">
            <span class="track-index">02</span>
            <span class="specimen-copy"><strong>Another track</strong><span>Artist · album</span></span>
            <span class="track-time">2:56</span>
          </div>
          <div class="solid-form">
            <label for={`search-${theme.id}`}>Search</label>
            <input id={`search-${theme.id}`} placeholder="Track, album, artist" />
          </div>
          <div class="specimen-actions">
            <Button material="solid" size="sm">Solid action</Button>
            <IconButton label="Поиск" material="solid"><Icon name="search" size={15} /></IconButton>
          </div>
          <p class="micro-note">lists, forms, lyrics: opaque/tonal, never blurred</p>
        </Surface>
      </div>
    </section>
  {/each}

  <footer class="preview-foot">
    <span>LG1 · tokens + materials.css · no app page changed</span>
    <span>?mode=lite · ?mode=fallback</span>
  </footer>
</div>

<style>
  .preview {
    width: min(1480px, calc(100% - 40px));
    margin: 0 auto;
    padding: 22px 0 56px;
    color: var(--text);
  }

  .preview-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--stroke);
  }

  .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .mark { color: var(--accent-2); display: grid; place-items: center; }
  .brand strong { display: block; font-size: 1rem; letter-spacing: -0.01em; }
  .brand span { display: block; color: var(--text-muted); font-size: var(--text-micro); letter-spacing: 0.08em; text-transform: uppercase; }

  .mode-switcher {
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: rgba(255, 255, 255, 0.035);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .theme-switcher {
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: rgba(255, 255, 255, 0.035);
  }

  .theme-switcher button,
  .mode-switcher button {
    padding: 7px 12px;
    border-radius: var(--radius-pill);
    color: var(--text-muted);
    font-size: var(--text-meta);
    font-weight: 600;
  }
  .theme-switcher button:hover,
  .mode-switcher button:hover { color: var(--text); }
  .theme-switcher button.active,
  .mode-switcher button.active { background: var(--accent-2); color: #071016; }

  .intro {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 28px;
    padding: 32px 0 16px;
  }

  .eyebrow {
    color: var(--accent-2);
    font-size: var(--text-micro);
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  h1 {
    margin: 7px 0 8px;
    font-size: clamp(2rem, 4vw, 3.4rem);
    line-height: 1.03;
    letter-spacing: -0.04em;
  }

  .intro p { max-width: 64ch; margin: 0; color: var(--text-soft); font-size: var(--text-body); }

  .budget {
    min-width: 210px;
    padding: 13px 16px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius);
    background: var(--material-content-fill);
  }
  .budget-label { display: block; color: var(--text-muted); font-size: var(--text-micro); letter-spacing: 0.1em; text-transform: uppercase; }
  .budget strong { display: block; margin: 3px 0 1px; color: var(--accent-2); font-size: 2rem; line-height: 1; }
  .budget strong small { color: var(--text-muted); font-size: 0.8rem; }
  .budget > span:last-child { color: var(--text-soft); font-size: var(--text-micro); }

  .mode-note {
    margin-bottom: 18px;
    padding: 9px 13px;
    border-left: 2px solid var(--accent-2);
    color: var(--text-soft);
    background: rgba(94, 231, 242, 0.055);
    font-size: var(--text-meta);
  }

  .stage {
    position: relative;
    overflow: hidden;
    margin: 0 0 22px;
    padding: clamp(18px, 3vw, 34px);
    border: 1px solid var(--stroke);
    border-radius: var(--radius-panel);
    color: var(--text);
  }

  .theme-dark {
    background:
      radial-gradient(700px 300px at 15% 0%, rgba(94, 231, 242, 0.12), transparent 72%),
      radial-gradient(800px 400px at 84% 100%, rgba(139, 92, 246, 0.18), transparent 74%),
      #070a10;
  }

  .theme-light { background: #e8edf5; color: #132033; }
  .theme-light .stage-kicker { color: #0b6674; }
  .theme-light .stage-hint { color: #55647a; }
  .theme-light h2 { color: #132033; }

  /* MusicNX is dark-first. On a light scene the same dark material keeps its
     contrast fill; a true light-glass variant is intentionally out of LG1. */
  .theme-light {
    --material-chrome-fill: rgba(5, 9, 15, 0.94);
    --material-overlay-fill: rgba(4, 7, 12, 0.96);
    --material-interactive-fill: rgba(8, 13, 21, 0.82);
    --material-interactive-hover: rgba(14, 21, 33, 0.9);
    --material-content-fill: rgba(10, 15, 23, 0.97);
    --material-solid: #0b121d;
    --material-edge: rgba(255, 255, 255, 0.28);
    --material-edge-soft: rgba(255, 255, 255, 0.18);
    --text: #f5f7fc;
    --text-soft: rgba(245, 247, 252, 0.82);
    --text-muted: #b6c1d4;
  }

  .theme-light .specimen-grid { color: var(--text); }

  .theme-cover {
    background:
      radial-gradient(560px 300px at 15% 25%, rgba(255, 72, 160, 0.85), transparent 68%),
      radial-gradient(540px 300px at 75% 10%, rgba(255, 173, 51, 0.8), transparent 68%),
      radial-gradient(700px 400px at 85% 90%, rgba(43, 213, 255, 0.78), transparent 70%),
      linear-gradient(125deg, #41206f, #10244e 52%, #0c0d1b);
  }

  .cover-art {
    position: absolute;
    right: 8%;
    top: 18%;
    width: 34%;
    aspect-ratio: 1;
    border-radius: 24px;
    background:
      linear-gradient(135deg, rgba(255, 255, 255, 0.28), transparent 42%),
      linear-gradient(315deg, #ff3c9a, #ffbe3c 48%, #55efff);
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
    opacity: 0.78;
    transform: rotate(-8deg);
    pointer-events: none;
  }

  .cover-disc {
    position: absolute;
    inset: 31%;
    border-radius: 50%;
    border: 1px solid rgba(255, 255, 255, 0.6);
    background: radial-gradient(circle, rgba(8, 12, 20, 0.8) 0 18%, transparent 19% 100%);
  }

  .cover-stripe {
    position: absolute;
    left: -25%;
    top: 46%;
    width: 150%;
    height: 13%;
    background: rgba(255, 255, 255, 0.18);
    transform: rotate(33deg);
  }

  .stage-head {
    position: relative;
    z-index: 3;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 18px;
  }

  .stage-kicker { color: var(--accent-2); font-size: var(--text-micro); font-weight: 700; letter-spacing: 0.16em; }
  h2 { margin: 4px 0 0; font-size: clamp(1.2rem, 2vw, 1.6rem); letter-spacing: -0.02em; }
  .stage-hint { color: var(--text-muted); font-size: var(--text-micro); white-space: nowrap; }

  .specimen-grid {
    position: relative;
    z-index: 3;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }

  :global(.specimen-grid .nx-surface) { min-width: 0; }

  .specimen-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px; }
  .material-label { color: var(--text-soft); font-size: 0.62rem; font-weight: 700; letter-spacing: 0.13em; }
  .material-label b { color: var(--accent-2); font-weight: 600; }
  .micro-note { margin: 11px 0 0; color: var(--text-muted); font-size: 0.67rem; line-height: 1.4; }

  .chrome-row { display: flex; align-items: center; gap: 10px; }
  .mini-cover { width: 42px; height: 42px; flex: none; border-radius: 10px; overflow: hidden; background: linear-gradient(140deg, #28d9c0, #3169d6 55%, #872fc4); }
  .mini-cover i { display: block; width: 9px; height: 9px; margin: 6px; border-radius: 50%; background: rgba(255,255,255,.6); }
  .mini-cover.alt { background: linear-gradient(140deg, #ffbc42, #e84393 55%, #6627a8); }
  .specimen-copy { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
  .specimen-copy strong { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 0.78rem; }
  .specimen-copy span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-muted); font-size: 0.68rem; }
  .specimen-actions { display: flex; align-items: center; gap: 7px; margin-top: 14px; flex-wrap: wrap; }
  .specimen-actions .micro-note { flex-basis: 100%; margin: 0; }

  .overlay-title { margin-bottom: 10px; font-size: 1.05rem; font-weight: 700; }
  .queue-sample { display: flex; align-items: center; gap: 9px; padding: 8px; border: 1px solid color-mix(in srgb, var(--cover-accent) 35%, transparent); border-radius: var(--radius-sm); background: var(--material-solid-soft); }
  .sample-count { margin-left: auto; color: var(--text-muted); font-size: var(--text-micro); }
  .solid-row { display: flex; align-items: center; gap: 8px; margin-top: 9px; padding: 9px 4px; color: var(--text-muted); font-size: var(--text-micro); border-bottom: 1px solid var(--stroke); }
  .row-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--accent-2); }

  .interactive-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .selected-chip { padding: 6px 9px; border: 1px solid color-mix(in srgb, var(--cover-accent) 55%, transparent); border-radius: var(--radius-pill); color: var(--accent-2); font-size: var(--text-micro); }
  .selected-swatch { display: flex; align-items: center; gap: 10px; margin-top: 12px; padding: 9px; border: 1px solid color-mix(in srgb, var(--cover-accent) 55%, transparent); border-radius: var(--radius-sm); box-shadow: var(--material-glow-selected); }
  .swatch-thumb { width: 34px; height: 34px; border-radius: 8px; background: linear-gradient(145deg, #ffbc42, #e84393 55%, #5ee7f2); }
  .selected-dot { width: 7px; height: 7px; margin-left: auto; border-radius: 50%; background: var(--cover-accent); box-shadow: 0 0 0 4px var(--cover-accent-soft); }

  .solid-track { display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 8px 2px; border-bottom: 1px solid var(--stroke); }
  .track-index, .track-time { color: var(--text-muted); font-size: var(--text-micro); font-variant-numeric: tabular-nums; }
  .solid-track.muted { opacity: 0.72; }
  .solid-form { display: flex; flex-direction: column; gap: 5px; margin-top: 12px; }
  .solid-form label { color: var(--text-muted); font-size: var(--text-micro); }
  .solid-form input { height: 32px; padding: 0 9px; border: 1px solid var(--stroke); border-radius: 8px; color: var(--text); background: rgba(255,255,255,.045); font-size: var(--text-meta); }

  .preview-foot { display: flex; justify-content: space-between; gap: 14px; padding-top: 12px; color: var(--text-muted); font-size: var(--text-micro); }

  @media (max-width: 760px) {
    .preview { width: calc(100% - 24px); padding-top: 14px; }
    .preview-head { align-items: flex-start; flex-direction: column; }
    .toolbar { width: 100%; }
    .theme-switcher, .mode-switcher { width: 100%; }
    .theme-switcher button, .mode-switcher button { flex: 1; }
    .intro { align-items: flex-start; flex-direction: column; gap: 16px; padding-top: 24px; }
    .budget { width: 100%; }
    .stage { border-radius: var(--radius-card); }
    .specimen-grid { grid-template-columns: 1fr; }
    .stage-head { align-items: flex-start; flex-direction: column; gap: 5px; }
    .stage-hint { display: none; }
    .cover-art { width: 68%; right: -12%; top: 14%; opacity: 0.45; }
    .preview-foot { flex-direction: column; }
  }

  @media (prefers-reduced-motion: reduce) {
    .cover-art { transition: none; }
  }
</style>
