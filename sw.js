// App-shell cache only (works under any sub-path, e.g. /solana-sentinel/). Live data never goes through here:
// it arrives over a WebSocket / cross-origin fetch to the relay, which the service worker does not touch.
const C = 'sentinel-pages-v2';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png', 'icons/favicon.png'];
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).catch(() => {})); });
self.addEventListener('activate', e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return; // relay, fonts, token images: network only
  const scope = new URL(self.registration.scope);
  if (u.pathname === scope.pathname || u.pathname.endsWith('/index.html')) { // page: network first, cached copy when offline
    e.respondWith(fetch(e.request).then(r => { const cp = r.clone(); if (r.ok) caches.open(C).then(c => c.put('./', cp)); return r; })
      .catch(() => caches.match('./')));
    return;
  }
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request).then(n => { const cp = n.clone(); if (n.ok) caches.open(C).then(c => c.put(e.request, cp)); return n; })));
});
