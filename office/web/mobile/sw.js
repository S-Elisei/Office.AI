const SHELL = ["/", "/assets/mobile.js", "/assets/mobile.css",
  "/assets/manifest.webmanifest", "/assets/icon-192.png", "/assets/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith("office-mobile-") && key !== CACHE).map((key) => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});

self.addEventListener("push", (event) => {
  const message = event.data.json();
  event.waitUntil(self.registration.showNotification(message.title, {
    body: message.body,
    icon: "/assets/icon-192.png",
    badge: "/assets/icon-192.png",
    tag: "office-dm:" + message.peer,
    renotify: true,
    data: {peer: message.peer, message_id: message.message_id},
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const peer = event.notification.data.peer;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    if (windows.length) {
      const window = await windows[0].focus();
      window.postMessage({type: "open-conversation", peer});
    } else {
      await self.clients.openWindow("/#" + new URLSearchParams({peer}));
    }
  })());
});
