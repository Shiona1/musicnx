import { mount } from 'svelte';
import './styles/tokens.css';
import './styles/global.css';
import './styles/materials.css';
import App from './App.svelte';

const app = mount(App, {
  target: document.getElementById('app')!,
});

// Lightweight TV mode: no backdrop blur; never changes behavior or data.
if (new URLSearchParams(window.location.search).get('lite') === '1') {
  document.body.classList.add('reduce-effects');
}

// Installable shell. The SW only caches static assets, never /api or /stream;
// in dev it stays disabled so Vite HMR is not shadowed by an old shell.
const isProduction = Boolean(
  (import.meta as ImportMeta & { env?: { PROD?: boolean } }).env?.PROD,
);
if ('serviceWorker' in navigator && isProduction) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => {
      // PWA is optional; a failed registration must not affect the player.
    });
  });
}

export default app;
