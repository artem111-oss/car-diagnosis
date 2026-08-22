// Кэширует только оболочку — HTML/иконки. API (/api/*) всегда идёт в сеть:
// диагноз не должен браться из кэша. Смысл — мгновенная повторная загрузка
// и рабочий мастер записи при слабом сигнале в гараже или на парковке.
const CACHE = "chtostuchit-shell-v1";
const SHELL = ["/", "/static/manifest.json", "/static/favicon.svg", "/static/logo.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // сеть всегда, без кэша

  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
