<script lang="ts">
  export let title: string | undefined = undefined;
  export let eyebrow: string | undefined = undefined;
  export let description: string | undefined = undefined;
  export let bare = false;
  export let size: 'default' | 'wide' | 'narrow' = 'default';
</script>

<main class="nx-page size-{size}" class:bare aria-labelledby={title ? 'nx-page-title' : undefined}>
  {#if !bare && title}
    <header class="page-head">
      <div class="page-copy">
        {#if eyebrow}<span class="eyebrow">{eyebrow}</span>{/if}
        <h1 id="nx-page-title">{title}</h1>
        {#if description}<p class="description">{description}</p>{/if}
      </div>
      <div class="page-actions"><slot name="actions" /></div>
    </header>
  {/if}
  <slot />
</main>

<style>
  .nx-page {
    width: 100%;
    max-width: var(--page-max);
    margin: 0 auto;
    padding: var(--section-gap) var(--page-gutter) calc(var(--dock-height) + var(--bnav-height) + 28px);
    animation: page-in var(--motion-page) var(--ease-out);
  }

  .nx-page.size-narrow { max-width: 980px; }
  .nx-page.size-wide { max-width: min(1840px, 100%); }
  .nx-page.bare { padding-top: clamp(18px, 2.8vw, 32px); }

  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
    flex-wrap: wrap;
    margin-bottom: clamp(20px, 2.4vw, 30px);
  }

  .page-copy { min-width: 0; }

  .eyebrow {
    display: block;
    margin-bottom: 5px;
    color: var(--accent-2);
    font-size: var(--text-micro);
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: var(--text-page);
    font-weight: 700;
    line-height: 1.08;
    letter-spacing: -0.03em;
  }

  .description {
    max-width: 72ch;
    margin: 8px 0 0;
    color: var(--text-soft);
    font-size: var(--text-body);
  }

  .page-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  @keyframes page-in {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .nx-page { animation: none; }
  }

  @media (max-width: 600px) {
    .nx-page { padding-bottom: calc(var(--dock-height) + var(--bnav-height) + 18px); }
    .page-head { align-items: flex-start; }
  }
</style>
