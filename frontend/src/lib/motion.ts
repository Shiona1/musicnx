import { fade } from 'svelte/transition';

function reducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

/** Shared dialog scrim transition; duration collapses under reduced motion. */
export function dialogFade(node: HTMLElement) {
  return fade(node, { duration: reducedMotion() ? 0 : 160 });
}
