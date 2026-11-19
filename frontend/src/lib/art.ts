import type { Availability, ArtistRef } from './types';

/** Deterministic cover gradient, no external artwork needed in mock mode. */
export function coverGradient(seed: string): string {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) % 360;
  }
  const hue = hash;
  const second = (hue + 52) % 360;
  return `linear-gradient(140deg, hsl(${hue} 62% 38%), hsl(${second} 68% 20%) 72%)`;
}

export function coverInitials(title: string): string {
  const words = title
    .replace(/[^\p{L}\p{N}\s-]/gu, ' ')
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return 'NX';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

export function artistNames(artists: ArtistRef[]): string {
  return artists.map((artist) => artist.name).join(', ') || 'Неизвестный исполнитель';
}

export function coverUrlAt(url: string | null | undefined, size: number): string | null {
  if (!url) return null;
  // Yandex CDN links go through the backend cover cache; direct browser access
  // would leak the user's IP and bypass the cache/LRU.
  if (/^https?:\/\/avatars\.yandex\.net\//i.test(url)) {
    return `/api/v1/yandex/cover?uri=${encodeURIComponent(url)}&size=${size}`;
  }
  return url.replace(/([?&]size=)\d+/, `$1${size}`);
}

export function coverBackground(url: string | null | undefined, seed: string): string {
  const sized = coverUrlAt(url, 300);
  return sized ? `background-image: url('${sized}');` : `background-image: ${coverGradient(seed)};`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (!ms || ms <= 0) return '—';
  const totalSeconds = Math.round(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function formatTime(ms: number): string {
  return formatDuration(ms);
}

export const AVAILABILITY_LABELS: Record<Availability, string> = {
  offline: 'Офлайн',
  online: 'Онлайн',
  metadata_only: 'Только метаданные',
  missing: 'Файл пропал',
  unavailable: 'Недоступно',
};

export function availabilityLabel(availability: Availability): string {
  return AVAILABILITY_LABELS[availability] ?? availability;
}

/** Russian plural forms: "1 трек / 2 трека / 5 треков". */
export function tracksWord(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return 'трек';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'трека';
  return 'треков';
}

/** Russian plural forms: "1 альбом / 2 альбома / 5 альбомов". */
export function albumsWord(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return 'альбом';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'альбома';
  return 'альбомов';
}

/** Russian plural forms: "1 исполнитель / 2 исполнителя / 5 исполнителей". */
export function artistsWord(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return 'исполнитель';
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'исполнителя';
  return 'исполнителей';
}
