export type View =
  | 'now'
  | 'library'
  | 'libraryAlbum'
  | 'libraryArtist'
  | 'yandex'
  | 'catalog'
  | 'setup'
  | 'downloads'
  | 'state'
  | 'sync'
  | 'zones'
  | 'settings'
  | 'network';

export type PrimarySection = 'now' | 'library' | 'yandex' | 'downloads' | 'system';
export type SystemPage = 'state' | 'sync' | 'zones' | 'network' | 'settings';

export type CatalogKind = 'playlist' | 'album' | 'artist' | 'search';

export interface CatalogTarget {
  kind: CatalogKind;
  /** Playlist `uid:kind`, album/artist id, or null for search. */
  id: string | null;
  /** Search query for `kind === "search"`. */
  query: string | null;
}

export interface LibraryTarget {
  kind: 'album' | 'artist';
  id: number;
}

const VIEW_PATHS: Record<View, string> = {
  now: '/',
  library: '/library',
  libraryAlbum: '/library/album',
  libraryArtist: '/library/artist',
  yandex: '/yandex',
  catalog: '/yandex/search',
  setup: '/yandex/setup',
  downloads: '/downloads',
  state: '/system/state',
  sync: '/system/sync',
  zones: '/system/zones',
  network: '/system/network',
  settings: '/system/settings',
};

export function pathForView(view: View): string {
  return VIEW_PATHS[view];
}

export function viewFromPath(pathname: string): View {
  const path = pathname.replace(/\/+$/, '') || '/';
  switch (path) {
    case '/library':
      return 'library';
    case '/yandex':
      return 'yandex';
    case '/yandex/setup':
      return 'setup';
    case '/yandex/search':
      return 'catalog';
    case '/downloads':
      return 'downloads';
    case '/system/state':
      return 'state';
    case '/system/sync':
      return 'sync';
    case '/system/zones':
      return 'zones';
    case '/system/network':
    case '/setup': // AP onboarding portal
      return 'network';
    case '/system/settings':
      return 'settings';
    default:
      if (/^\/yandex\/(playlist|album|artist)\/.+/.test(path)) return 'catalog';
      if (/^\/library\/album\/\d+/.test(path)) return 'libraryAlbum';
      if (/^\/library\/artist\/\d+/.test(path)) return 'libraryArtist';
      return 'now';
  }
}

/** Build a URL for a catalog page. */
export function catalogPath(target: CatalogTarget): string {
  if (target.kind === 'search') {
    return `/yandex/search?q=${encodeURIComponent(target.query ?? '')}`;
  }
  return `/yandex/${target.kind}/${encodeURIComponent(target.id ?? '')}`;
}

/** Parse `/yandex/{playlist|album|artist}/{id}` or `/yandex/search?q=…`. */
export function parseCatalogPath(
  pathname: string,
  search = '',
): CatalogTarget | null {
  const path = pathname.replace(/\/+$/, '') || '/';
  if (path === '/yandex/search') {
    const query = new URLSearchParams(search).get('q')?.trim() ?? '';
    return { kind: 'search', id: null, query };
  }
  const match = path.match(/^\/yandex\/(playlist|album|artist)\/(.+)$/);
  if (match === null) return null;
  const id = decodeURIComponent(match[2]);
  if (!id) return null;
  return { kind: match[1] as 'playlist' | 'album' | 'artist', id, query: null };
}

/** Build a URL for a local library detail page. */
export function libraryPath(target: LibraryTarget): string {
  return `/library/${target.kind}/${target.id}`;
}

/** Parse `/library/{album|artist}/{id}`. */
export function parseLibraryPath(pathname: string): LibraryTarget | null {
  const match = pathname
    .replace(/\/+$/, '')
    .match(/^\/library\/(album|artist)\/(\d+)$/);
  if (match === null) return null;
  return { kind: match[1] as 'album' | 'artist', id: Number(match[2]) };
}

export function sectionForView(view: View): PrimarySection {
  if (
    view === 'catalog' ||
    view === 'state' ||
    view === 'sync' ||
    view === 'zones' ||
    view === 'settings' ||
    view === 'network'
  ) {
    return view === 'catalog' ? 'yandex' : 'system';
  }
  if (view === 'setup') return 'yandex';
  if (view === 'libraryAlbum' || view === 'libraryArtist') return 'library';
  return view;
}

export function systemPageForView(view: View): SystemPage {
  if (
    view === 'state' ||
    view === 'zones' ||
    view === 'network' ||
    view === 'settings'
  ) {
    return view;
  }
  return 'sync';
}

export function isApPortalPath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '') === '/setup';
}
