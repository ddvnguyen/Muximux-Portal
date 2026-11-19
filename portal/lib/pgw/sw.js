// pgw service worker (M-323 port map, M-421 portal PWA cache model).
//
// Two jobs, in this order:
//   1. Keep the portal shell installable and bootable: precache `/`, the
//      shell's hashed `/assets/*`, `/pwa/*` and `/offline.html`; serve `/`
//      stale-while-revalidate so a cold launch paints from disk.
//   2. Keep the M-323 port mapping: a client inside `/p/<port>/` gets its
//      root-absolute requests (`/api`, `/_next`, ...) rewritten back under
//      `/p/<port>/`, and the hashed build output of each app is cached under
//      `app-<port>-<build>` so a return visit does not re-download it.
//
// Cache policy (M-421 condition 1): cache-first ONLY for hashed assets and
// `/pwa/*` plus `/offline.html`; `/` is stale-while-revalidate. App HTML is
// cached only when the gateway says it may be (`X-PWA-Cache: 1`, i.e. the
// port has `pwa_cache: true` and the upstream response is not
// no-store/private/redirected/Set-Cookie) — see portal-gw `gw()`.
const PRE = /^\/p\/(\d+)(\/|$)/;
const MAP_CACHE = 'pgw';
const SHELL_CACHE = 'portal-shell';
const APP_PREFIX = 'app-';
const OFFLINE_URL = '/offline.html';
const SHELL_URL = '/';
const PRECACHE = [
  SHELL_URL,
  OFFLINE_URL,
  '/pwa/icon-maskable-512.png',
  '/android-chrome-192x192.png',
  '/android-chrome-512x512.png',
];
// Vite/rolldown and Next both put the content hash in the filename. Requiring
// 6+ url-safe chars after a separator keeps `index.js` out of the cache-first
// path (a non-hashed name can change in place).
const HASHED_RE = /(?:[.-][A-Za-z0-9_-]{6,})\.[a-z0-9]+$/i;
const NEXT_STATIC_RE = /^\/_next\/static\//;

// -- M-323: clientId -> port -------------------------------------------------
const map = new Map();
const builds = new Map();
let loaded = null;
function load() {
  return loaded || (loaded = Promise.all([
    caches.open(MAP_CACHE).then(c => c.match('/pgw-map')).then(r => r ? r.json() : {}).then(o => {
      for (const k in o) if (!map.has(k)) map.set(k, o[k]);
    }).catch(() => {}),
    caches.open(MAP_CACHE).then(c => c.match('/pgw-builds')).then(r => r ? r.json() : {}).then(o => {
      for (const k in o) if (!builds.has(+k)) builds.set(+k, o[k]);
    }).catch(() => {}),
  ]).catch(() => {}));
}
function save() {
  return caches.open(MAP_CACHE).then(c => c.put('/pgw-map', new Response(JSON.stringify(Object.fromEntries(map))))).catch(() => {});
}
function saveBuilds() {
  return caches.open(MAP_CACHE).then(c => c.put('/pgw-builds', new Response(JSON.stringify(Object.fromEntries(builds))))).catch(() => {});
}
function setp(id, p) { if (id && map.get(id) !== p) { map.set(id, p); save(); } }
function setBuild(port, build) {
  if (build && builds.get(port) !== build) { builds.set(port, build); saveBuilds(); }
}
function buildFor(port) {
  const b = builds.get(port);
  return b && /^[A-Za-z0-9_-]+$/.test(b) ? b : '0';
}
function appCacheName(port) { return APP_PREFIX + port + '-' + buildFor(port); }
function appCachePrefix(port) { return APP_PREFIX + port + '-'; }

async function portFor(e) {
  await load();
  let p = map.get(e.clientId);
  if (p) return p;
  // Parser-inserted subresources of a navigating document can arrive with
  // an empty clientId before the new client exists (M-323): fall back to
  // the navigation's own port (request Referer is /p/<port>/... then).
  // NOTE: FetchEvent has no .referrer — it lives on e.request (a Request).
  if (!e.clientId && e.request.referrer) {
    try {
      const rm = new URL(e.request.referrer).pathname.match(PRE);
      if (rm) return +rm[1];
    } catch { /* malformed referrer: fall through */ }
  }
  if (e.clientId) {
    const c = await self.clients.get(e.clientId);
    if (c) { const m = new URL(c.url).pathname.match(PRE); if (m) { setp(e.clientId, +m[1]); return +m[1]; } }
  }
  return null;
}

// -- M-421: install / activate ----------------------------------------------
async function precache() {
  const cache = await caches.open(SHELL_CACHE);
  // `/` carries the shell HTML; cache it first so the asset list can be read
  // out of it. Every entry is best-effort: one 404 must not fail install
  // (that is exactly what killed the worker in M-337).
  const results = await Promise.allSettled([...PRECACHE].map(u => cache.add(u)));
  const shell = results[0];
  if (shell.status === 'fulfilled') {
    try {
      const res = await cache.match(SHELL_URL);
      const html = res ? await res.text() : '';
      const assets = new Set();
      for (const m of html.matchAll(/(?:src|href)="(\.{0,2}\/assets\/[^"]+)"/g)) {
        try { assets.add(new URL(m[1], self.location.origin).pathname); } catch { /* skip */ }
      }
      await Promise.allSettled([...assets].map(u => cache.add(u)));
    } catch { /* asset list is an optimisation only */ }
  }
}

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    try { await precache(); } catch { /* never block install on precache */ }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    try {
      const keys = await caches.keys();
      await Promise.all(keys
        .filter(k => k !== SHELL_CACHE && k !== MAP_CACHE && !k.startsWith(APP_PREFIX))
        .map(k => caches.delete(k)));
    } catch { /* cache storage unavailable */ }
    await self.clients.claim();
  })());
});

// -- M-421: cache helpers ----------------------------------------------------
function isHashed(pathname) { return NEXT_STATIC_RE.test(pathname) || HASHED_RE.test(pathname); }
function isShellPath(pathname) {
  if (pathname === '/' || pathname === '/index.html') return true;
  if (pathname === OFFLINE_URL) return true;
  if (pathname.startsWith('/pwa/')) return true;
  if (pathname.startsWith('/assets/')) return isHashed(pathname);
  return false;
}
function storable(res) {
  if (!res || !res.ok || res.redirected) return false;
  // Forbidden response header names are not exposed to fetch(), so the
  // authoritative Set-Cookie/no-store decision is the gateway's
  // X-PWA-Cache header; these checks are defence in depth.
  if (res.headers.has('Set-Cookie')) return false;
  const cc = (res.headers.get('Cache-Control') || '').toLowerCase();
  return !/(?:^|,)\s*(?:no-store|private)\b/.test(cc);
}
// waitUntil() is only legal while the event is being dispatched; the
// background work below starts after an await, so guard the call (Chrome
// keeps the worker alive for the respondWith promise either way).
function keepAlive(e, p) { try { e.waitUntil(Promise.resolve(p).catch(() => {})); } catch { /* already settled */ } }

async function offline() {
  const hit = await caches.match(OFFLINE_URL);
  if (hit) return hit;
  return new Response('offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
}

async function matchApp(port, req) {
  // Only look at caches that already exist: `caches.open()` would create an
  // empty `app-<port>-0` cache on a miss.
  const name = appCacheName(port);
  const prefix = appCachePrefix(port);
  const keys = (await caches.keys()).filter(k => k.startsWith(prefix));
  keys.sort((a, b) => (b === name ? 1 : 0) - (a === name ? 1 : 0));
  for (const key of keys) {
    const hit = await caches.open(key).then(c => c.match(req)).catch(() => null);
    if (hit) return hit;
  }
  return null;
}

async function storeApp(port, req, res) {
  // Remember the build even when the document itself may not be stored:
  // hashed assets requested after this navigation must land in the current
  // build's cache, not in `app-<port>-0`.
  const header = res.headers.get('X-App-Build');
  if (header) setBuild(port, header);
  // The gateway sets X-PWA-Cache: 1 only for ports with `pwa_cache: true`
  // whose response carries no Set-Cookie and no no-store/private.
  if (res.headers.get('X-PWA-Cache') !== '1' || !storable(res)) return;
  const build = res.headers.get('X-App-Build') || '0';
  const name = APP_PREFIX + port + '-' + (/^[A-Za-z0-9_-]+$/.test(build) ? build : '0');
  try {
    const cache = await caches.open(name);
    await cache.put(req, res);
    // New build written; drop the port's older build caches (M-421 §4).
    for (const key of await caches.keys()) {
      if (key.startsWith(appCachePrefix(port)) && key !== name) await caches.delete(key);
    }
    setBuild(port, build);
  } catch { /* quota / opaque response */ }
}

async function storeAsset(port, req, res) {
  if (!res || !res.ok || res.redirected) return;
  const cc = (res.headers.get('Cache-Control') || '').toLowerCase();
  if (/(?:^|,)\s*no-store\b/.test(cc)) return;
  try {
    const cache = await caches.open(appCacheName(port));
    await cache.put(req, res);
  } catch { /* quota */ }
}

// -- fetch -------------------------------------------------------------------
self.addEventListener('fetch', e => {
  const req = e.request;
  const u = new URL(req.url);
  if (u.origin !== self.location.origin) return; // M-341: cross-origin skips the SW entirely
  const m = u.pathname.match(PRE);

  if (!m && isShellPath(u.pathname)) {
    e.stopImmediatePropagation(); // Muximux's network-first handler must not also respondWith
    if (u.pathname === OFFLINE_URL || u.pathname.startsWith('/pwa/')) {
      e.respondWith((async () => (await caches.match(req)) || fetch(req))());
      return;
    }
    // `/` and hashed `/assets/*`: stale-while-revalidate (M-421 condition 1).
    e.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const hit = await cache.match(req);
      const network = fetch(req).then(res => {
        if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
        return res;
      });
      if (hit) { keepAlive(e, network.catch(() => {})); return hit; }
      try { return await network; } catch { return offline(); }
    })());
    return;
  }

  if (m) {
    const port = +m[1];
    if (req.mode === 'navigate') {
      // A claimed top-level deep link may redirect from /p/N/... to the
      // portal shell. Do not label the resulting shell client as an app.
      e.stopImmediatePropagation();
      e.respondWith((async () => {
        // M-421: stale-while-revalidate only for ports the gateway marked
        // cacheable. The marker survives in the cached response headers, so
        // a cache hit is self-describing.
        const hit = req.method === 'GET' ? await matchApp(port, req) : null;
        if (hit && hit.headers.get('X-PWA-Cache') === '1') {
          keepAlive(e, (async () => {
            try {
              const res = await fetch(req);
              const final = new URL(res.url).pathname.match(PRE);
              if (!final || +final[1] !== port) return;
              await storeApp(port, req, res.clone());
            } catch { /* offline: keep serving the cached document */ }
          })());
          return hit;
        }
        try {
          const res = await fetch(req);
          const final = new URL(res.url).pathname.match(PRE);
          if (final) setp(e.resultingClientId, +final[1]);
          if (res.ok && final) keepAlive(e, storeApp(port, req, res.clone()));
          return res;
        } catch {
          return offline();
        }
      })());
      return;
    }
    // Hashed build output only: keyed by URL inside app-<port>-<build>, so a
    // new deploy has new names and no stale risk.
    if (req.method === 'GET' && isHashed(u.pathname)) {
      e.stopImmediatePropagation();
      e.respondWith((async () => {
        const hit = await matchApp(port, req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) keepAlive(e, storeAsset(port, req, res.clone()));
        return res;
      })());
    }
    return;
  }

  // Root-absolute request from an app frame (M-323): rewrite to its port.
  e.respondWith((async () => {
    try {
      const port = await portFor(e);
      if (!port) { return fetch(req); }
      const nu = '/p/' + port + u.pathname + u.search;
      // Deny list: never cache, never buffer — pass straight through. The
      // rewrite still applies, otherwise POST /api from an app frame would
      // hit the portal origin instead of the app.
      if (req.method !== 'GET' ||
          (req.headers.get('Accept') || '').includes('text/event-stream') ||
          req.headers.has('Authorization') ||
          /(?:^|,)\s*no-store\b/i.test(req.headers.get('Cache-Control') || '')) {
        return fetch(nu, { method: req.method, headers: req.headers,
                           credentials: req.credentials, cache: 'no-store' });
      }
      if (isHashed(u.pathname)) {
        const hit = await matchApp(port, req);
        if (hit) return hit;
        const res = await fetch(nu);
        if (res.ok) keepAlive(e, storeAsset(port, req, res.clone()));
        return res;
      }
      const r = req, init = { method: r.method, headers: r.headers, credentials: r.credentials, redirect: r.redirect };
      if (r.method !== 'GET' && r.method !== 'HEAD') init.body = r.body;
      init.duplex = 'half';
      return await fetch(nu, init);
    } catch (err) { return new Response('sw error: ' + err, { status: 598 }); }
  })());
});
