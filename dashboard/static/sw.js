// Parental Proxy dashboard service worker.
//
// Served at /sw.js (not /static/sw.js -- see the dedicated Flask route in
// dashboard.py) so its default scope is the whole app, not just /static/.
//
// This exists to satisfy PWA installability (a registered service worker
// with a fetch handler), not to provide real offline use: this is an admin
// tool that approves/blocks a child's actual internet access, so serving a
// stale cached page while "offline" would be actively misleading, not a
// convenience. Only our own static assets (versioned by CACHE below) are
// cached; every dashboard page and every form submission always goes to
// the network, no exceptions.
const CACHE = "pp-static-v1";
const STATIC_ASSETS = [
  "/static/css/app.css",
  "/static/vendor/chart.umd.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
  }
  // Everything else (every page, every POST) is left to the browser's
  // normal network handling -- deliberately no offline fallback.
});
