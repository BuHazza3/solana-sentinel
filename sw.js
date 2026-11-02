// App-shell cache only (works under any sub-path, e.g. /solana-sentinel/). Live data never goes through here:
// it arrives over a WebSocket / cross-origin fetch to the relay, which the service worker does not touch.
// The page itself is network-first and bypasses the HTTP cache, so a new version shows up on the next open.
const C = 'sentinel-pages-v5';
const SHELL = ['./', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png', 'icons/favicon.png'];
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(SHELL.map(u => new Request(u, {cache: 'reload'})))).catch(() => {})); });
self.addEventListener('activate', e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('message', e => { if (e.data === 'skipWaiting') self.skipWaiting(); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return; // relay, fonts, charts, token images: network only
  const scope = new URL(self.registration.scope);
  if (u.pathname.endsWith('/version.json') || u.pathname.endsWith('/sw.js')) return; // always network
  if (e.request.mode === 'navigate' || u.pathname === scope.pathname || u.pathname.endsWith('/index.html')) { // page: network first (no HTTP cache), cached copy offline
    e.respondWith(fetch(u.pathname + u.search, {cache: 'no-store', credentials: 'same-origin'})
      .then(r => { if (r.ok) { const cp = r.clone(); caches.open(C).then(c => c.put('./', cp)); } return r; })
      .catch(() => caches.match('./').then(r => r || Response.error())));
    return;
  }
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then(n => { const cp = n.clone(); if (n.ok) caches.open(C).then(c => c.put(e.request, cp)); return n; })));
});
