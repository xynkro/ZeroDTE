// ZeroDTE PWA service worker.
// Strategy: cache the app shell so the dashboard launches instantly / offline,
// but NEVER cache /api or /ws — live data must always hit the network (no stale
// trades/quotes). This also satisfies the installability criteria (HTTPS + SW
// with a fetch handler + manifest).
const CACHE = 'zerodte-shell-v2';
// Relative so the shell caches correctly whether served from the backend root
// (:8765/) or a GitHub Pages project subpath (/ZeroDTE/).
const SHELL = ['./', './manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Live data: always network, never cache.
  if (url.pathname.startsWith('/api') || url.pathname === '/ws') return;
  // Only handle same-origin GETs (let cross-origin CDN libs pass through).
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  // App shell: network-first, fall back to cache (then to '/') when offline.
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(e.request).then((m) => m || caches.match('./')))
  );
});
