<script lang="ts">
  import { coverGradient, coverInitials, coverUrlAt } from '../lib/art';

  /** One artwork surface used by every shelf/queue/Now Playing view. */
  export let url: string | null | undefined = null;
  export let title = 'MusicNX';
  export let alt = '';
  export let size = 300;
  export let eager = false;
  export let className = '';

  let failed = false;
  $: src = coverUrlAt(url, size);
  $: seed = `${title}|${url ?? ''}`;
  $: showImage = Boolean(src) && !failed;
  $: fallbackStyle = `background-image: ${coverGradient(seed)};`;
</script>

{#if showImage}
  <img
    class="cover-art {className}"
    src={src}
    {alt}
    loading={eager ? 'eager' : 'lazy'}
    decoding="async"
    draggable="false"
    onerror={() => (failed = true)}
  />
{:else}
  <span class="cover-fallback {className}" style={fallbackStyle} aria-hidden="true">
    {coverInitials(title)}
  </span>
{/if}

<style>
  .cover-art,
  .cover-fallback {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    background-size: cover;
    background-position: center;
  }

  .cover-fallback {
    display: grid;
    place-items: center;
    color: rgba(255, 255, 255, 0.82);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    text-shadow: 0 2px 18px rgba(0, 0, 0, 0.35);
  }
</style>
