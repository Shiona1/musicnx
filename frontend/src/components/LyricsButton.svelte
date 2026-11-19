<script lang="ts">
  /**
   * P5 toggle for `LyricsPanel`: renders the control only, the parent owns the
   * open state (App/NowPlaying keeps `showLyrics` and mounts the panel). Keeping
   * the state outside means the panel can be closed without losing the fetch
   * rules of the shared lyrics store, and both components stay testable alone.
   */
  export let open = false;
  export let disabled = false;
  export let label = 'Текст';
  export let onToggle: () => void = () => {};

  $: ariaLabel = open ? 'Скрыть текст песни' : 'Показать текст песни';
  $: title = disabled ? 'Текст недоступен без выбранного трека' : ariaLabel;
</script>

<button
  type="button"
  class="lyrics-toggle"
  class:active={open}
  aria-pressed={open}
  aria-label={ariaLabel}
  {title}
  {disabled}
  onclick={onToggle}
>
  <svg
    viewBox="0 0 24 24"
    width="16"
    height="16"
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M4 5h11v2H4zm0 5h11v2H4zm0 5h11v2H4zm14.4-7.5v9.1a2.4 2.4 0 1 0 1.6 2.25V7.5z" />
  </svg>
  <span class="label">{label}</span>
</button>

<style>
  .lyrics-toggle {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 7px 13px;
    min-height: 34px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.76rem;
    font-weight: 700;
    max-width: 100%;
    transition:
      background 140ms ease,
      border-color 140ms ease,
      color 140ms ease,
      transform 140ms ease;
  }

  .lyrics-toggle:hover:not(:disabled) {
    background: var(--surface-hover);
    border-color: rgba(34, 211, 238, 0.35);
    color: #a5f3fc;
    transform: translateY(-1px);
  }

  .lyrics-toggle.active {
    color: #a5f3fc;
    border-color: rgba(34, 211, 238, 0.4);
    background: rgba(34, 211, 238, 0.1);
  }

  .lyrics-toggle:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 3px;
  }

  .label {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  @media (max-width: 560px) {
    .lyrics-toggle {
      padding: 9px 15px;
      min-height: 40px;
    }
  }
</style>
