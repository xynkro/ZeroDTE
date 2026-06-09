// ZeroDTE Terminal v2 — service worker. App-shell cache so the terminal launches
// instantly / offline. NEVER caches /api or /ws (live data must hit the network).
const CACHE = 'zerodte-v2-v6';
const SHELL = ['./', './app.js', './manifest.webmanifest', './wordmark.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api') || url.pathname === '/ws') return;       // live data → network only
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return; // let CDN/raw pass through
  e.respondWith(
    fetch(e.request)
      .then((resp) => { const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {}); return resp; })
      .catch(() => caches.match(e.request).then((m) => m || caches.match('./')))
  );
});
