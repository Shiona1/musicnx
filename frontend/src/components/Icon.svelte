<script lang="ts">
  export let name: string;
  export let size = 20;
</script>

<svg
  class="icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="currentColor"
  aria-hidden="true"
  focusable="false"
>
  {#if name === 'play'}
    <path d="M8 5v14l11-7z" />
  {:else if name === 'pause'}
    <path d="M6 5h4v14H6zm8 0h4v14h-4z" />
  {:else if name === 'previous'}
    <path d="M6 6h2v12H6zm3.5 6L18 6v12z" />
  {:else if name === 'next'}
    <path d="M16 6h2v12h-2zM14.5 12 6 18V6z" />
  {:else if name === 'shuffle'}
    <path d="M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z" />
  {:else if name === 'repeat'}
    <path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z" />
  {:else if name === 'repeat-one'}
    <path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4zm-4-2V9h-1l-2 1v1h1.5v4H13z" />
  {:else if name === 'chevron-up'}
    <path d="M12 7.2 5.3 13.9l1.4 1.4L12 9.9l5.3 5.4 1.4-1.4z" />
  {:else if name === 'volume'}
    <path d="M4 9v6h4l5 4V5L8 9H4zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z" />
  {:else if name === 'plus'}
    <path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z" />
  {:else if name === 'close'}
    <path d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z" />
  {:else if name === 'queue'}
    <path d="M4 6h16v2H4zm0 5h10v2H4zm0 5h10v2H4zm12 0v6l5-3z" />
  {:else if name === 'music'}
    <path d="M9 18a3 3 0 1 1-2-2.83V5.4l12-2v12.6a3 3 0 1 1-2-2.83V7.5L9 8.8z" />
  {:else if name === 'stop'}
    <path d="M6 6h12v12H6z" />
  {:else if name === 'search'}
    <path d="M10.5 4a6.5 6.5 0 1 0 4.05 11.59l4.43 4.43 1.42-1.42-4.43-4.43A6.5 6.5 0 0 0 10.5 4zm0 2a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9z" />
  {:else if name === 'chevron-left'}
    <path d="M14.7 5.3 8 12l6.7 6.7 1.4-1.4L10.8 12l5.3-5.3z" />
  {:else if name === 'chevron-right'}
    <path d="M9.3 5.3 8 6.7l5.3 5.3L8 17.3l1.3 1.4L16 12z" />
  {:else if name === 'menu'}
    <path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z" />
  {:else if name === 'chevron-down'}
    <path d="M5.3 8.3 12 15l6.7-6.7-1.4-1.4L12 12.2 6.7 6.9z" />
  {:else if name === 'dots'}
    <path d="M6 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4z" />
  {:else if name === 'wifi'}
    <path d="M1 9l2 2c4.97-4.97 13.03-4.97 18 0l2-2C16.93 2.93 7.08 2.93 1 9zm8 8l3 3 3-3a4.24 4.24 0 0 0-6 0zm-4-4l2 2a7.07 7.07 0 0 1 10 0l2-2C15.14 9.14 8.87 9.14 5 13z" />
  {:else if name === 'wifi-off'}
    <path d="M2.3 3.7 1 5l2.3 2.3C2.4 7.9 1.6 8.5 1 9l2 2c.8-.8 1.7-1.4 2.7-1.9L8 11.4A9 9 0 0 0 5 13l2 2c1.1-1 2.5-1.6 4-1.8L22.9 24l1.4-1.4L2.3 3.7zM9 17l3 3 2.2-2.2-2.2-2.2L9 17zm6.4-6.5c2 .5 3.9 1.6 5.6 3.1l2-2a13.9 13.9 0 0 0-5.4-3.3l-2.2 2.2zM12 3C7.8 3 3.9 4.6 1 7.3l2 2A16 16 0 0 1 12 5c1.3 0 2.6.2 3.8.5l2.1-2.1A16.6 16.6 0 0 0 12 3z" />
  {:else if name === 'bluetooth'}
    <path d="M17.71 7.71 12 2h-1v7.59L6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 11 14.41V22h1l5.71-5.71-4.3-4.29 4.3-4.29zM13 5.83l1.88 1.88L13 9.59V5.83zm1.88 10.46L13 18.17v-3.76l1.88 1.88z" />
  {:else if name === 'speaker'}
    <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06a9 9 0 0 0 0-17.54z" />
  {:else if name === 'monitor'}
    <path d="M3 4h18v12H3V4zm2 2v8h14V6H5zm1 12h12v2H6v-2z" />
  {:else if name === 'lock'}
    <path d="M12 2a5 5 0 0 0-5 5v3H5v12h14V10h-2V7a5 5 0 0 0-5-5zm-3 8V7a3 3 0 0 1 6 0v3H9zm3 3a2 2 0 0 1 1 3.73V19h-2v-2.27A2 2 0 0 1 12 13z" />
  {:else if name === 'check'}
    <path d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
  {:else if name === 'download'}
    <path d="M11 3h2v9.17l3.59-3.58L18 10l-6 6-6-6 1.41-1.41L11 12.17V3zM5 19h14v2H5z" />
  {:else if name === 'ethernet'}
    <path d="M4 6h16v12H4V6zm2 2v8h12V8H6zm1 1h2v2H7V9zm4 0h2v2h-2V9zm4 0h2v2h-2V9zM7 13h2v2H7v-2zm4 0h2v2h-2v-2zm4 0h2v2h-2v-2z" />
  {:else if name === 'power'}
    <path d="M11 2h2v10h-2V2zm-3.6 2.4 1.4 1.4A6 6 0 1 0 18 12a6 6 0 0 0-2.8-5.1l1.4-1.4A8 8 0 1 1 7.4 4.4z" />
  {:else if name === 'restart'}
    <path d="M12 5V2L7 7l5 5V9a5 5 0 1 1-4.9 6h-2A7 7 0 1 0 12 5z" />
  {:else if name === 'thermometer'}
    <path d="M12 2a3 3 0 0 0-3 3v7.2a5 5 0 1 0 6 0V5a3 3 0 0 0-3-3zm0 2a1 1 0 0 1 1 1v8.1l.6.3a3 3 0 1 1-3.2 0l.6-.3V5a1 1 0 0 1 1-1z" />
  {:else if name === 'cpu'}
    <path d="M9 2h2v2h2V2h2v2h2a1 1 0 0 1 1 1v2h2v2h-2v2h2v2h-2v2a1 1 0 0 1-1 1h-2v2h-2v-2h-2v2H9v-2H7a1 1 0 0 1-1-1v-2H4v-2h2v-2H4V9h2V5a1 1 0 0 1 1-1h2V2zm-1 5v10h8V7H8zm2 2h4v6h-4V9z" />
  {:else if name === 'memory'}
    <path d="M4 6h16v10H4V6zm2 2v6h2v-2h2v2h2v-2h2v2h2v-2h2V8h-2v2h-2V8h-2v2h-2V8H8v2H6V8H6zM6 18h2v2H6v-2zm5 0h2v2h-2v-2zm5 0h2v2h-2v-2z" />
  {:else if name === 'activity'}
    <path d="M3 12h4l2-6 4 12 2-6h6v2h-4.5l-2.7 8-3.8-11.4L8.8 14H3v-2z" />
  {:else if name === 'disk'}
    <path d="M4 5h16v6H4V5zm2 2v2h2V7H6zm10 0v2h2V7h-2zM4 13h16v6H4v-6zm2 2v2h2v-2H6zm10 0v2h2v-2h-2z" />
  {/if}
</svg>

<style>
  .icon {
    display: block;
    flex: none;
  }
</style>
