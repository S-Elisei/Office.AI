"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  keys: null, publicKey: "", device: "", owner: "", peer: null, conversations: [],
  messages: [], pending: null, connected: false, connecting: null, active: true,
  socket: null, ready: false, requests: new Map(), sequence: 0, sending: false,
  view: 0, loading: false, seen: new Map(), retryTimer: null, retryDelay: 1000,
  registration: null, push: null, pushBusy: false,
  targetPeer: new URLSearchParams(location.hash.slice(1)).get("peer"),
};
let storage;

function stored(mode, operation) {
  return new Promise((resolve, reject) => {
    const tx = storage.transaction("device", mode);
    const result = operation(tx.objectStore("device"));
    tx.oncomplete = () => resolve(result.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

async function prepareKey() {
  storage = await new Promise((resolve, reject) => {
    const request = indexedDB.open("office-mobile", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("device");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  state.keys = await stored("readonly", (store) => store.get("keys"));
  if (!state.keys) {
    state.keys = await crypto.subtle.generateKey(
      {name: "ECDSA", namedCurve: "P-256"}, false, ["sign", "verify"]
    );
    await stored("readwrite", (store) => store.put(state.keys, "keys"));
  }
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", state.keys.publicKey));
  state.publicKey = "office-p256:" + btoa(String.fromCharCode(...raw));
  state.device = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", raw)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
  state.pending = await stored("readonly", (store) => store.get("pending"));
  $("pairing-key").value = state.publicKey;
  $("device-key").value = state.publicKey;
  $("fingerprint").textContent = "Fingerprint: " + state.device;
  await navigator.storage.persist?.();
}

function showError(error) {
  $("error").textContent = error.message;
  $("error").hidden = false;
}

function rejectRequests(error) {
  for (const request of state.requests.values()) {
    clearTimeout(request.timer);
    request.reject(error);
  }
  state.requests.clear();
}

function visiblePeer() {
  if (document.visibilityState !== "visible") return null;
  const panels = [...document.querySelectorAll("main > section, dialog[open]")]
    .filter((panel) => panel.getClientRects().length);
  const workspace = $("workspace");
  return panels.length === 1 && panels[0] === workspace &&
    workspace.querySelector(".chat").getClientRects().length ? state.peer : null;
}

function reportView(peer = visiblePeer()) {
  if (!state.ready || state.socket.readyState !== WebSocket.OPEN) return;
  state.socket.send(JSON.stringify({id: ++state.sequence, op: "view", data: {peer}}));
}

function stopSocket() {
  reportView(null);
  const socket = state.socket;
  state.socket = null;
  state.ready = false;
  socket?.close();
  rejectRequests(new Error("Connection interrupted. Retry the send to check delivery."));
  updateComposer();
  updatePush();
}

function sessionEnded(message) {
  state.active = false;
  state.connected = false;
  clearTimeout(state.retryTimer);
  stopSocket();
  $("workspace").hidden = true;
  $("connect-panel").hidden = false;
  $("connection").textContent = "Disconnected";
  $("connect-status").textContent = message;
}

function scheduleReconnect() {
  clearTimeout(state.retryTimer);
  if (!state.active || !navigator.onLine || document.visibilityState !== "visible") return;
  state.retryTimer = setTimeout(connect, state.retryDelay);
  state.retryDelay = Math.min(60000, state.retryDelay * 2);
}

function request(op, data = {}) {
  if (!state.ready) return Promise.reject(new Error("Connection unavailable. Reconnect and retry."));
  const id = ++state.sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      state.requests.delete(id);
      reject(new Error("Reply not received. Retry the send to check delivery."));
      state.socket?.close();
    }, 20000);
    state.requests.set(id, {resolve, reject, timer});
    state.socket.send(JSON.stringify({id, op, data}));
  });
}

function openSocket() {
  stopSocket();
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(location.origin.replace(/^http/, "ws") + "/api/socket");
    state.socket = socket;
    const timer = setTimeout(() => {
      reject(new Error("Connection timed out. Check your network."));
      socket.close();
    }, 20000);
    socket.onopen = () => socket.send(JSON.stringify({device: state.device}));
    socket.onmessage = async (event) => {
      if (state.socket !== socket) return;
      const message = JSON.parse(event.data);
      if (message.event === "challenge") {
        try {
          const signature = await crypto.subtle.sign(
            {name: "ECDSA", hash: "SHA-256"}, state.keys.privateKey,
            new TextEncoder().encode(message.data.message)
          );
          socket.send(JSON.stringify({challenge: message.data.challenge,
            signature: btoa(String.fromCharCode(...new Uint8Array(signature)))}));
        } catch (error) { clearTimeout(timer); reject(error); socket.close(); }
      } else if (message.event === "ready") {
        clearTimeout(timer);
        state.ready = true;
        reportView();
        resolve(message.data);
      } else if (message.event === "error" || message.event === "unauthorized") {
        clearTimeout(timer);
        const error = new Error(message.detail);
        error.status = message.status ?? 401;
        if (state.ready) {
          stopSocket();
          connect();
        } else reject(error);
      } else if (message.event === "message") {
        receiveMessage(message);
      } else if (message.event === "conversations") {
        applyConversations(message.data);
        updateComposer();
      } else {
        const pending = state.requests.get(message.id);
        if (!pending) return;
        clearTimeout(pending.timer);
        state.requests.delete(message.id);
        if (message.status === 200) pending.resolve(message.data);
        else {
          const error = new Error(message.detail);
          error.status = message.status;
          pending.reject(error);
        }
      }
    };
    socket.onclose = () => {
      clearTimeout(timer);
      const error = new Error("Connection interrupted. Retry the send to check delivery.");
      reject(error);
      if (state.socket !== socket) return;
      state.socket = null;
      state.ready = false;
      rejectRequests(error);
      updateComposer();
      updatePush();
      $("connection").textContent = navigator.onLine ? "Reconnecting…" : "Offline";
      scheduleReconnect();
    };
  });
}

function connect() {
  state.active = true;
  if (state.connecting) return state.connecting;
  clearTimeout(state.retryTimer);
  $("connect-button").disabled = true;
  $("logout-button").disabled = true;
  state.connecting = (async () => {
    try {
      const snapshot = await openSocket();
      state.connected = true;
      state.retryDelay = 1000;
      applyConversations(snapshot);
      $("connect-panel").hidden = true;
      $("workspace").hidden = false;
      reportView();
      $("connection").textContent = "Connected";
      $("error").hidden = true;
      const peer = state.pending?.peer ?? state.targetPeer ?? state.peer ??
        state.conversations.find((item) => item.kind === "director")?.name ?? "all";
      state.targetPeer = null;
      if (peer !== state.peer || !state.messages.length) await openConversation(peer);
      else await syncMessages();
      if (state.pending) {
        $("message-body").value = state.pending.body;
        $("send-status").textContent = "Unconfirmed send. Retry to check delivery.";
      }
      updateComposer();
      await refreshPush();
    } catch (error) {
      if (error.status === 401) sessionEnded(error.message);
      else {
        $("connection").textContent = navigator.onLine ? "Reconnecting…" : "Offline";
        $("connect-status").textContent = "Connection unavailable. Reconnecting…";
        if (navigator.onLine && document.visibilityState === "visible") showError(error);
        scheduleReconnect();
      }
    } finally {
      state.connecting = null;
      $("connect-button").disabled = false;
      $("logout-button").disabled = false;
    }
  })();
  return state.connecting;
}

function applyConversations(data) {
  state.owner = data.owner;
  state.conversations = data.conversations;
  renderConversations();
}

function mergeMessages(rows) {
  const messages = new Map(state.messages.map((message) => [message.id, message]));
  for (const message of rows) messages.set(message.id, message);
  state.messages = [...messages.values()].sort((a, b) => a.id - b.id);
}

function receiveMessage(event) {
  const index = state.conversations.findIndex((peer) => peer.name === event.conversation.name);
  if (index < 0) state.conversations.push(event.conversation);
  else state.conversations[index] = event.conversation;
  state.conversations.sort((a, b) => (a.kind !== "director") - (b.kind !== "director") || b.last_id - a.last_id);
  renderConversations();
  if (event.conversation.name !== state.peer) return;
  const scroll = $("message-scroll");
  const stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
  mergeMessages([event.message]);
  if (!state.loading) {
    renderMessages(stick);
    markSeen().catch(showError);
  }
}

function renderConversations() {
  const fragment = document.createDocumentFragment();
  for (const peer of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation" + (peer.name === state.peer ? " active" : "");
    button.disabled = Boolean(state.pending && state.pending.peer !== peer.name);
    button.setAttribute("aria-current", peer.name === state.peer ? "page" : "false");
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = peer.kind === "common" ? "#" : peer.name.slice(0, 1).toUpperCase();
    const labels = document.createElement("span");
    labels.className = "conversation-labels";
    const name = document.createElement("span");
    name.className = "conversation-name";
    name.textContent = peer.kind === "common" ? "Common chat" : peer.name;
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.textContent = peer.preview || (peer.kind === "director" ? "Director" : "Start a conversation");
    labels.append(name, preview);
    button.append(avatar, labels);
    if (peer.unread) {
      const badge = document.createElement("span");
      badge.className = "unread";
      badge.textContent = peer.unread;
      button.append(badge);
    }
    button.addEventListener("click", () => openConversation(peer.name).catch(showError));
    fragment.append(button);
  }
  $("conversations").replaceChildren(fragment);
}

function renderMessages(stick) {
  const fragment = document.createDocumentFragment();
  for (const message of state.messages) {
    const article = document.createElement("article");
    article.className = "message" + (message.sender === state.owner ? " mine" : "");
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const sender = document.createElement("strong");
    sender.textContent = message.sender;
    const time = document.createElement("time");
    time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleString("en", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    meta.append(sender, time);
    const body = document.createElement("div");
    body.className = "message-body";
    body.textContent = message.body;
    article.append(meta, body);
    fragment.append(article);
  }
  $("messages").replaceChildren(fragment);
  $("empty-chat").hidden = state.messages.length !== 0;
  if (stick) $("message-scroll").scrollTop = $("message-scroll").scrollHeight;
}

function updateComposer() {
  const peer = state.conversations.find((item) => item.name === state.peer);
  $("composer").hidden = !peer?.can_send && !state.pending;
  $("message-body").readOnly = Boolean(state.pending);
  $("send-button").disabled = state.sending || !state.ready;
  $("send-button").textContent = state.sending ? "Sending…" : state.pending ? "Retry send" : "Send";
}

async function openConversation(peer) {
  if (state.pending && state.pending.peer !== peer) {
    showError(new Error("Retry the pending send before opening another conversation."));
    return;
  }
  const view = ++state.view;
  const changed = state.peer !== peer;
  state.peer = peer;
  state.loading = true;
  state.messages = [];
  $("messages").replaceChildren();
  $("older-button").hidden = true;
  $("empty-chat").hidden = true;
  if (changed) $("message-body").value = "";
  $("workspace").classList.add("show-chat");
  reportView();
  const item = state.conversations.find((entry) => entry.name === peer);
  $("chat-title").textContent = peer === "all" ? "Common chat" : peer;
  $("chat-subtitle").textContent = peer === "all" ? "Everyone in the office" :
    item?.kind === "director" ? "Director" : item?.status ?? "";
  renderConversations();
  updateComposer();
  try {
    const data = await request("messages", {peer});
    if (view !== state.view) return;
    mergeMessages(data.messages);
    $("older-button").hidden = !data.has_older;
    renderMessages(true);
  } finally {
    if (view === state.view) state.loading = false;
  }
  await markSeen();
}

async function syncMessages() {
  const view = state.view;
  const peer = state.peer;
  const scroll = $("message-scroll");
  const stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
  let after = state.messages.at(-1)?.id ?? 0;
  let more;
  do {
    const data = await request("messages", {peer, after_id: after});
    if (view !== state.view) return;
    mergeMessages(data.messages);
    more = data.has_newer;
    if (more) after = data.messages.at(-1).id;
  } while (more);
  renderMessages(stick);
  await markSeen();
}

async function markSeen() {
  if (!state.ready || state.loading || !state.messages.length || visiblePeer() !== state.peer) return;
  const scroll = $("message-scroll");
  if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight > 80) return;
  const peer = state.peer;
  const message = state.messages.at(-1);
  if (state.seen.get(peer) === message.id) return;
  await request("seen", {peer, message_id: message.id});
  state.seen.set(peer, message.id);
  const item = state.conversations.find((entry) => entry.name === peer);
  if (item) item.unread = 0;
  renderConversations();
}

function updatePush() {
  const supported = "PushManager" in window && "Notification" in window;
  $("push-button").hidden = !supported;
  $("push-button").disabled = !state.ready || !state.registration || !state.push || state.pushBusy;
  $("push-button").textContent = state.push?.endpoint ? "Disable notifications" : "Enable notifications";
  $("push-status").textContent = !supported ? "Notifications require a browser with Web Push support." :
    Notification.permission === "denied" ? "Notifications are blocked in browser settings." :
    state.push?.endpoint ? "Direct-message notifications are enabled." : "Enable notifications for incoming direct messages.";
}

async function refreshPush() {
  state.push = await request("push");
  if (state.registration && state.push.endpoint) {
    const subscription = await state.registration.pushManager.getSubscription();
    if (!subscription) state.push = await request("push_unsubscribe");
    else if (subscription.endpoint !== state.push.endpoint) {
      state.push = await request("push_subscribe", subscription.toJSON());
    }
  }
  updatePush();
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending) return;
  const body = $("message-body").value.trim();
  if (!body) return;
  const newSend = !state.pending;
  if (newSend) state.pending = {request_id: crypto.randomUUID(), peer: state.peer, body};
  state.sending = true;
  updateComposer();
  renderConversations();
  try {
    if (newSend) {
      await stored("readwrite", (store) => store.put(state.pending, "pending"));
    }
    $("send-status").textContent = "Sending…";
    const message = await request("send", state.pending);
    mergeMessages([message]);
    renderMessages(true);
    await stored("readwrite", (store) => store.delete("pending"));
    state.pending = null;
    $("message-body").value = "";
    $("send-status").textContent = "Sent";
    $("error").hidden = true;
    await markSeen();
  } catch (error) {
    if (error.status === 409) {
      await stored("readwrite", (store) => store.delete("pending"));
      state.pending = null;
      $("send-status").textContent = "Send refused. Choose a conversation and send again.";
    } else $("send-status").textContent = "Delivery unconfirmed. Retry uses the same send ID.";
    showError(error);
  } finally {
    state.sending = false;
    updateComposer();
    renderConversations();
  }
}

$("connect-button").addEventListener("click", connect);
$("composer").addEventListener("submit", sendMessage);
$("back-button").addEventListener("click", () => {
  $("workspace").classList.remove("show-chat");
  reportView();
});
$("device-button").addEventListener("click", () => {
  $("device-dialog").showModal();
  reportView();
});
$("close-device").addEventListener("click", () => $("device-dialog").close());
$("device-dialog").addEventListener("close", () => reportView());
for (const button of document.querySelectorAll(".copy-key")) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(state.publicKey);
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = "Copy public key"; }, 2000);
    } catch (error) { showError(error); }
  });
}
$("logout-button").addEventListener("click", () => {
  $("device-dialog").close();
  sessionEnded("Signed out. Connect to use this phone again.");
});
$("push-button").addEventListener("click", async () => {
  state.pushBusy = true;
  updatePush();
  try {
    if (state.push.endpoint) {
      state.push = await request("push_unsubscribe");
      const subscription = await state.registration.pushManager.getSubscription();
      if (subscription) await subscription.unsubscribe();
    } else {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") return;
      const existing = await state.registration.pushManager.getSubscription();
      if (existing) await existing.unsubscribe();
      const encoded = state.push.public_key.replace(/-/g, "+").replace(/_/g, "/");
      const key = Uint8Array.from(atob(encoded + "=".repeat((4 - encoded.length % 4) % 4)), (char) => char.charCodeAt(0));
      const subscription = await state.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      state.push = await request("push_subscribe", subscription.toJSON());
    }
  } catch (error) { showError(error); }
  finally { state.pushBusy = false; updatePush(); }
});
$("older-button").addEventListener("click", async () => {
  const view = state.view;
  const scroll = $("message-scroll");
  const height = scroll.scrollHeight;
  $("older-button").disabled = true;
  try {
    const data = await request("messages", {peer: state.peer, before_id: state.messages[0].id});
    if (view !== state.view) return;
    mergeMessages(data.messages);
    $("older-button").hidden = !data.has_older;
    renderMessages(false);
    scroll.scrollTop += scroll.scrollHeight - height;
  } catch (error) { showError(error); }
  finally { $("older-button").disabled = false; }
});
let seenTimer;
$("message-scroll").addEventListener("scroll", () => {
  clearTimeout(seenTimer);
  seenTimer = setTimeout(() => markSeen().catch(showError), 200);
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    clearTimeout(state.retryTimer);
    stopSocket();
  } else {
    reportView();
    if (state.active) connect();
  }
});
window.addEventListener("resize", () => reportView());
window.addEventListener("online", () => {
  if (state.active && document.visibilityState === "visible") connect();
});
window.addEventListener("offline", () => {
  clearTimeout(state.retryTimer);
  stopSocket();
  $("connection").textContent = "Offline";
});

(async () => {
  $("connect-button").disabled = true;
  updatePush();
  try {
    await prepareKey();
    $("connect-button").disabled = false;
    $("connect-status").textContent = "Ready to register.";
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).then((registration) => {
        state.registration = registration;
        if (state.ready) refreshPush().catch(showError);
        else updatePush();
      }).catch(showError);
      navigator.serviceWorker.addEventListener("message", (event) => {
        if (event.data.type !== "open-conversation") return;
        state.targetPeer = event.data.peer;
        if (state.ready) {
          state.targetPeer = null;
          openConversation(event.data.peer).catch(showError);
        } else if (state.active && document.visibilityState === "visible") connect();
      });
    }
    if (document.visibilityState === "visible") await connect();
  } catch (error) {
    $("connect-status").textContent = "Could not prepare this phone's key. Allow site storage and reload.";
    showError(error);
  }
})();
