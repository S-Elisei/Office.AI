"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  keys: null, publicKey: "", device: "", owner: "", peer: null, conversations: [],
  messages: [], pending: null, connected: false, connecting: false, session: 0, sending: false, source: null,
  view: 0, loading: false, refreshing: false, refreshAgain: false, refreshTimer: null, seenTimer: null,
  seen: new Map(),
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
  if (error.status === 401) return;
  $("error").textContent = error.message;
  $("error").hidden = false;
}

function sessionEnded(message) {
  state.connected = false;
  state.source?.close();
  state.source = null;
  $("workspace").hidden = true;
  $("connect-panel").hidden = false;
  $("connection").textContent = "Disconnected";
  $("connect-status").textContent = message;
}

async function reSignIn(session) {
  if (state.connected && !state.connecting && session === state.session) await connect();
}

async function api(path, method = "GET", data) {
  const session = state.session;
  const options = {method, cache: "no-store", signal: AbortSignal.timeout(20000)};
  if (method !== "GET") {
    options.headers = {"Content-Type": "application/json", "X-Office-Mobile": "1"};
    options.body = JSON.stringify(data ?? {});
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Office returned ${response.status}. Reconnect and retry.`;
    if (response.headers.get("content-type")?.includes("application/json")) {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    }
    if (response.status === 401) await reSignIn(session);
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function connect() {
  const silent = state.connected;
  state.connecting = true;
  state.source?.close();
  state.source = null;
  $("connect-button").disabled = true;
  $("logout-button").disabled = true;
  if (!silent) {
    $("connect-status").textContent = "Connecting…";
    $("error").hidden = true;
  }
  try {
    const challenge = await api("/api/challenge", "POST", {device: state.device});
    const signature = await crypto.subtle.sign(
      {name: "ECDSA", hash: "SHA-256"}, state.keys.privateKey,
      new TextEncoder().encode(challenge.message)
    );
    await api("/api/session", "POST", {
      challenge: challenge.challenge, signature: btoa(String.fromCharCode(...new Uint8Array(signature))),
    });
    state.session += 1;
    state.connected = true;
    if (!silent) {
      $("connect-panel").hidden = true;
      $("workspace").hidden = false;
      await refreshConversations();
      const initial = state.pending?.peer ?? state.peer ??
        state.conversations.find((peer) => peer.kind === "director")?.name ?? "all";
      await openConversation(initial);
      if (state.pending) {
        $("message-body").value = state.pending.body;
        $("send-status").textContent = "Unconfirmed send. Retry to check delivery.";
      }
      updateComposer();
    }
    $("connection").textContent = "Connected";
    listen();
  } catch (error) {
    sessionEnded(error.status === 401 ? error.message : "Could not connect. Check your connection and retry.");
    showError(error);
  } finally {
    state.connecting = false;
    $("connect-button").disabled = false;
    $("logout-button").disabled = false;
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

async function refreshConversations() {
  const data = await api("/api/conversations");
  state.owner = data.owner;
  state.conversations = data.conversations;
  renderConversations();
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
  $("send-button").disabled = state.sending;
  $("send-button").textContent = state.sending ? "Sending…" : state.pending ? "Retry send" : "Send";
}

async function openConversation(peer) {
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
  const item = state.conversations.find((entry) => entry.name === peer);
  $("chat-title").textContent = peer === "all" ? "Common chat" : peer;
  $("chat-subtitle").textContent = peer === "all" ? "Everyone in the office" :
    item?.kind === "director" ? "Director" : item?.status ?? "";
  renderConversations();
  updateComposer();
  try {
    const data = await api("/api/messages?" + new URLSearchParams({peer}));
    if (view !== state.view) return;
    state.messages = data.messages;
    $("older-button").hidden = !data.has_older;
    renderMessages(true);
    await markSeen();
  } finally {
    if (view === state.view) state.loading = false;
  }
  if (state.refreshAgain) refresh();
}

async function markSeen() {
  if (!state.connected || document.visibilityState !== "visible" || !state.messages.length) return;
  if (matchMedia("(max-width: 700px)").matches && !$("workspace").classList.contains("show-chat")) return;
  const scroll = $("message-scroll");
  if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight > 80) return;
  const peer = state.peer;
  const message = state.messages.at(-1);
  if (state.seen.get(peer) === message.id) return;
  await api("/api/seen", "POST", {peer, message_id: message.id});
  state.seen.set(peer, message.id);
  const item = state.conversations.find((entry) => entry.name === peer);
  if (item) item.unread = 0;
  renderConversations();
}

async function refresh() {
  if (!state.connected || document.visibilityState !== "visible") return;
  if (state.loading) { state.refreshAgain = true; return; }
  if (state.refreshing) { state.refreshAgain = true; return; }
  state.refreshing = true;
  try {
    do {
      state.refreshAgain = false;
      const view = state.view;
      const peer = state.peer;
      await refreshConversations();
      if (view !== state.view || state.loading || !state.connected) continue;
      if (peer) {
        const scroll = $("message-scroll");
        const stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
        let more;
        do {
          const data = await api("/api/messages?" + new URLSearchParams({
            peer, after_id: state.messages.at(-1)?.id ?? 0,
          }));
          if (view !== state.view) break;
          state.messages.push(...data.messages);
          more = data.has_newer;
        } while (more);
        if (view === state.view) {
          renderMessages(stick);
          updateComposer();
          await markSeen();
        }
      }
    } while (state.refreshAgain && state.connected);
    if (state.connected) {
      $("connection").textContent = "Connected";
      $("error").hidden = true;
    }
  } catch (error) {
    if (state.connected && error.status !== 401) {
      $("connection").textContent = "Reconnecting…";
      showError(error);
    }
  } finally {
    state.refreshing = false;
  }
}

function listen() {
  state.source?.close();
  const source = new EventSource("/api/events");
  const session = state.session;
  state.source = source;
  source.addEventListener("refresh", () => {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = setTimeout(refresh, 100);
  });
  source.addEventListener("revoked", () => reSignIn(session));
  source.onopen = () => { $("connection").textContent = "Connected"; };
  source.onerror = async () => {
    if (!state.connected || state.source !== source) return;
    $("connection").textContent = "Reconnecting…";
    try {
      await api("/api/conversations");
    } catch (error) { showError(error); }
    if (state.connected && state.source === source && source.readyState === EventSource.CLOSED) {
      setTimeout(() => { if (state.connected && state.source === source) listen(); }, 3000);
    }
  };
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending) return;
  const body = $("message-body").value.trim();
  if (!body) return;
  state.sending = true;
  try {
    if (!state.pending) {
      state.pending = {request_id: crypto.randomUUID(), peer: state.peer, body};
      await stored("readwrite", (store) => store.put(state.pending, "pending"));
    }
    updateComposer();
    renderConversations();
    $("send-status").textContent = "Sending…";
    await api("/api/messages", "POST", state.pending);
    await stored("readwrite", (store) => store.delete("pending"));
    state.pending = null;
    $("message-body").value = "";
    $("send-status").textContent = "Sent";
    await refresh();
  } catch (error) {
    if (error.status === 409) {
      await stored("readwrite", (store) => store.delete("pending"));
      state.pending = null;
      $("send-status").textContent = "Send refused. Choose a conversation and send again.";
    } else {
      $("send-status").textContent = "Delivery unconfirmed. Retry uses the same send ID.";
    }
    showError(error);
  } finally {
    state.sending = false;
    updateComposer();
    renderConversations();
  }
}

$("connect-button").addEventListener("click", connect);
$("composer").addEventListener("submit", sendMessage);
$("back-button").addEventListener("click", () => $("workspace").classList.remove("show-chat"));
$("device-button").addEventListener("click", () => $("device-dialog").showModal());
$("close-device").addEventListener("click", () => $("device-dialog").close());
for (const button of document.querySelectorAll(".copy-key")) {
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(state.publicKey);
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = "Copy public key"; }, 2000);
    } catch (error) { showError(error); }
  });
}
$("logout-button").addEventListener("click", async () => {
  $("device-dialog").close();
  sessionEnded("Signed out. Connect to use this phone again.");
  try {
    await api("/api/session", "DELETE");
  } catch (error) { showError(error); }
});
$("older-button").addEventListener("click", async () => {
  const view = state.view;
  const scroll = $("message-scroll");
  const height = scroll.scrollHeight;
  $("older-button").disabled = true;
  try {
    const data = await api("/api/messages?" + new URLSearchParams({
      peer: state.peer, before_id: state.messages[0].id,
    }));
    if (view !== state.view) return;
    state.messages.unshift(...data.messages);
    $("older-button").hidden = !data.has_older;
    renderMessages(false);
    scroll.scrollTop += scroll.scrollHeight - height;
  } catch (error) { showError(error); }
  finally { $("older-button").disabled = false; }
});
$("message-scroll").addEventListener("scroll", () => {
  clearTimeout(state.seenTimer);
  state.seenTimer = setTimeout(() => markSeen().catch(showError), 200);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.connected) {
    listen();
    refresh();
  }
});
window.addEventListener("online", () => {
  if (state.connected) { listen(); refresh(); }
});
window.addEventListener("offline", () => { $("connection").textContent = "Offline"; });

(async () => {
  $("connect-button").disabled = true;
  try {
    await prepareKey();
    $("connect-button").disabled = false;
    $("connect-status").textContent = "Ready to register.";
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(showError);
    }
    await connect();
  } catch (error) {
    $("connect-status").textContent = "Could not prepare this phone's key. Allow site storage and reload.";
    showError(error);
  }
})();
