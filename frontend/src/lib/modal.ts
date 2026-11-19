/**
 * Shared modal-dialog helpers: focus restore, Tab trapping and field autofocus.
 * Mirrors the QueueDrawer dialog behavior so every dialog in the app treats
 * keyboard and screen-reader users the same way.
 */

/** Autofocus a form field once the dialog mounts (used as `use:focusInput`). */
export function focusInput(node: HTMLInputElement): { destroy: () => void } {
  const timer = window.setTimeout(() => node.focus(), 0);
  return { destroy: () => window.clearTimeout(timer) };
}

/**
 * Remember the element focused before the dialog opened and restore it when
 * the dialog unmounts. Attach to the dialog backdrop with `use:keepFocus`.
 */
export function keepFocus(_node: HTMLElement): { destroy: () => void } {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  return {
    destroy(): void {
      previous?.focus();
    },
  };
}

/**
 * Keep Tab navigation inside the dialog (WAI-ARIA focus trap). Attach as
 * `onkeydown={trapTabKey}` on the element carrying role="dialog".
 */
export function trapTabKey(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return;
  const root = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  if (!root) return;
  const focusable = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
