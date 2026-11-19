import { coverUrlAt } from './art';

export interface CoverAccent {
  color: string;
  soft: string;
  strong: string;
}

type Cached = CoverAccent | null;

const cache = new Map<string, Cached>();

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const sector = Math.floor(h / 60) % 6;
  const pairs: [number, number, number][] = [
    [c, x, 0],
    [x, c, 0],
    [0, c, x],
    [0, x, c],
    [x, 0, c],
    [c, 0, x],
  ];
  const [r, g, b] = pairs[sector];
  return [
    Math.round((r + m) * 255),
    Math.round((g + m) * 255),
    Math.round((b + m) * 255),
  ];
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const delta = max - min;
  const l = (max + min) / 2;
  if (delta === 0) return [0, 0, l];
  const s = delta / (1 - Math.abs(2 * l - 1));
  let h = 0;
  if (max === rn) h = 60 * (((gn - bn) / delta) % 6);
  else if (max === gn) h = 60 * ((bn - rn) / delta + 2);
  else h = 60 * ((rn - gn) / delta + 4);
  if (h < 0) h += 360;
  return [h, s, l];
}

/** Keep the accent in a small, music-friendly palette so covers cannot create
 * a neon/washed-out shell. Fall back to MusicNX cyan for monochrome covers. */
function paletteHue(hue: number): number {
  const palette = [188, 205, 262, 305];
  return palette.reduce((best, candidate) => {
    const distance = Math.min(Math.abs(candidate - hue), 360 - Math.abs(candidate - hue));
    const bestDistance = Math.min(Math.abs(best - hue), 360 - Math.abs(best - hue));
    return distance < bestDistance ? candidate : best;
  }, palette[0]);
}

function makeAccent(hue: number, saturation: number, lightness: number): CoverAccent {
  const h = paletteHue(hue);
  const s = Math.min(0.72, Math.max(0.52, saturation));
  // Keep black-on-accent chrome text above WCAG AA even for violet covers.
  const l = Math.min(0.7, Math.max(0.66, lightness));
  const [r, g, b] = hslToRgb(h, s, l);
  return {
    color: `rgb(${r}, ${g}, ${b})`,
    soft: `rgba(${r}, ${g}, ${b}, 0.14)`,
    strong: `rgba(${r}, ${g}, ${b}, 0.34)`,
  };
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = 'async';
    image.crossOrigin = 'anonymous';
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('cover load failed'));
    image.src = url;
  });
}

/**
 * Read-only color extraction from a same-origin cover.
 * It samples a 32x32 canvas once per URL and never runs per animation frame.
 */
export async function getCoverAccent(coverUrl: string | null | undefined): Promise<CoverAccent | null> {
  if (!coverUrl || typeof document === 'undefined') return null;
  const sized = coverUrlAt(coverUrl, 64);
  if (!sized) return null;
  if (cache.has(sized)) return cache.get(sized) ?? null;

  try {
    const image = await loadImage(sized);
    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 32;
    const context = canvas.getContext('2d', { willReadFrequently: false });
    if (!context) {
      cache.set(sized, null);
      return null;
    }
    context.drawImage(image, 0, 0, 32, 32);
    const data = context.getImageData(0, 0, 32, 32).data;
    let r = 0;
    let g = 0;
    let b = 0;
    let count = 0;
    for (let index = 0; index < data.length; index += 4) {
      const alpha = data[index + 3];
      if (alpha < 128) continue;
      const pr = data[index];
      const pg = data[index + 1];
      const pb = data[index + 2];
      const [, saturation, lightness] = rgbToHsl(pr, pg, pb);
      // Ignore near-white/near-black pixels; they carry no usable tint.
      if (saturation < 0.12 || lightness < 0.1 || lightness > 0.9) continue;
      const weight = 0.35 + saturation;
      r += pr * weight;
      g += pg * weight;
      b += pb * weight;
      count += weight;
    }
    if (count === 0) {
      cache.set(sized, null);
      return null;
    }
    const [hue, saturation, lightness] = rgbToHsl(r / count, g / count, b / count);
    if (saturation < 0.1) {
      cache.set(sized, null);
      return null;
    }
    const accent = makeAccent(hue, saturation + 0.18, lightness);
    cache.set(sized, accent);
    return accent;
  } catch {
    cache.set(sized, null);
    return null;
  }
}

/** Set or clear the runtime accent on the document root. */
export function setCoverAccent(accent: CoverAccent | null): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (!accent) {
    root.style.removeProperty('--cover-accent');
    root.style.removeProperty('--cover-accent-soft');
    root.style.removeProperty('--cover-accent-strong');
    return;
  }
  root.style.setProperty('--cover-accent', accent.color);
  root.style.setProperty('--cover-accent-soft', accent.soft);
  root.style.setProperty('--cover-accent-strong', accent.strong);
}
