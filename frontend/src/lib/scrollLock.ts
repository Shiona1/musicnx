/**
 * Page scroll lock for full-screen overlays (karaoke, queue).
 *
 * There must be exactly one writer: App.svelte calls `setScrollLocked` from its
 * reactive statement. Components must not touch `body.style.overflow` — a
 * cleanup race between two writers used to leave the page permanently locked.
 *
 * Chrome resets the scroll position when `overflow: hidden` is applied to the
 * root element, so the position is saved before locking and restored after
 * unlocking.
 */

let locked = false;
let savedScrollY = 0;

export function setScrollLocked(next: boolean): void {
  if (next === locked) return;
  locked = next;
  const root = document.documentElement;
  if (next) {
    savedScrollY = window.scrollY;
    root.classList.add('scroll-locked');
    return;
  }
  root.classList.remove('scroll-locked');
  const restoreTo = savedScrollY;
  // Wait for the scroll container to become scrollable again.
  requestAnimationFrame(() => window.scrollTo(0, restoreTo));
}
