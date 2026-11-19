<script lang="ts">
  /**
   * Minimal dependency-free line chart for the system status page.
   * Null samples are skipped; the x axis is the sample index, not wall time,
   * because the sampler uses a fixed interval.
   */
  export let values: (number | null)[] = [];
  export let height = 72;
  export let min: number | null = null;
  export let max: number | null = null;
  export let stroke = 'var(--accent-2)';
  export let fill = true;

  const gradientId = `spark-${Math.random().toString(36).slice(2, 9)}`;

  $: numeric = values.filter((value): value is number => value !== null);
  $: low = min ?? (numeric.length ? Math.min(...numeric) : 0);
  $: high = max ?? (numeric.length ? Math.max(...numeric) : 1);
  $: span = Math.max(high - low, 0.01);
  $: path = buildPath(values, low, span);

  function buildPath(items: (number | null)[], floor: number, range: number): string {
    const step = 600 / Math.max(items.length - 1, 1);
    let path = '';
    let open = false;
    items.forEach((value, index) => {
      if (value === null || !Number.isFinite(value)) {
        open = false;
        return;
      }
      const x = (index * step).toFixed(1);
      const y = (height - ((value - floor) / range) * (height - 8) - 4).toFixed(1);
      path += `${open ? 'L' : 'M'}${x},${y}`;
      open = true;
    });
    return path;
  }
</script>

<svg
  class="spark"
  viewBox={`0 0 600 ${height}`}
  preserveAspectRatio="none"
  role="img"
  aria-label="График"
>
  {#if fill && path}
    <defs>
      <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color={stroke} stop-opacity="0.28" />
        <stop offset="100%" stop-color={stroke} stop-opacity="0" />
      </linearGradient>
    </defs>
    <path d={`${path} L600,${height} L0,${height} Z`} fill={`url(#${gradientId})`} />
  {/if}
  {#if path}
    <path d={path} fill="none" stroke={stroke} stroke-width="2" vector-effect="non-scaling-stroke" />
  {/if}
</svg>

<style>
  .spark {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }
</style>
