<script lang="ts">
  export let title: string;
  export let description: string | undefined = undefined;
  export let actionLabel: string | undefined = undefined;
  export let onAction: (() => void) | undefined = undefined;
</script>

<div class="empty-state">
  <div class="empty-art" aria-hidden="true">
    <span></span><span></span><span></span>
  </div>
  <div class="empty-copy">
    <strong>{title}</strong>
    {#if description}<p>{description}</p>{/if}
  </div>
  {#if actionLabel && onAction}
    <button type="button" class="empty-action" onclick={onAction}>{actionLabel}</button>
  {/if}
</div>

<style>
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 14px;
    padding: 24px;
    border: 1px dashed var(--stroke);
    border-radius: var(--radius);
    background: rgba(255, 255, 255, 0.018);
  }

  .empty-art { display: flex; align-items: flex-end; gap: 4px; height: 24px; opacity: 0.5; }
  .empty-art span { width: 5px; border-radius: 3px; background: var(--accent-2); }
  .empty-art span:nth-child(1) { height: 45%; }
  .empty-art span:nth-child(2) { height: 100%; }
  .empty-art span:nth-child(3) { height: 68%; }

  .empty-copy strong { display: block; color: var(--text); font-size: var(--text-body); }
  .empty-copy p { margin: 4px 0 0; max-width: 52ch; color: var(--text-muted); font-size: var(--text-meta); }

  .empty-action {
    padding: 7px 12px;
    border: 1px solid var(--stroke);
    border-radius: var(--radius-pill);
    background: var(--surface);
    color: var(--text-soft);
    font-size: var(--text-meta);
    font-weight: 600;
  }
  .empty-action:hover { background: var(--surface-hover); color: var(--text); }
</style>
