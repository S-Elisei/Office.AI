"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  keys: null, publicKey: "", device: "", owner: "", peer: null, conversations: [],
  messages: [], pending: null, connected: false, connecting: null, active: true,
  socket: null, ready: false, requests: new Map(), sequence: 0, sending: false,
  view: 0, loading: false, seen: new Map(), retryTimer: null, retryDelay: 1000,
  registration: null, push: null, pushBusy: false,
  targetPeer: new URLSearchParams(location.hash.slice(1)).get("peer"),
  office: null, officeBusy: false, works: null, serverOffset: 0, worksClock: null,
  workBusy: new Set(), reportedView: null, activities: new Map(),
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

function visiblePanel() {
  if (document.visibilityState !== "visible") return null;
  const panels = [...document.querySelectorAll("main > section, dialog[open]")]
    .filter((panel) => panel.getClientRects().length);
  return panels.length === 1 ? panels[0] : null;
}

function visiblePeer() {
  const workspace = $("workspace");
  return visiblePanel() === workspace &&
    workspace.querySelector(".chat").getClientRects().length ? state.peer : null;
}

function reportView(peer = visiblePeer()) {
  if (!state.ready || state.socket.readyState !== WebSocket.OPEN) return;
  const works = document.visibilityState === "visible" && !$("works-page").hidden;
  const outputs = works ? [...$("works-list").querySelectorAll("details[data-output][open]")]
    .map((details) => Number(details.dataset.output)) : [];
  const data = {peer, works, quota: visiblePanel() === $("quota-page"), outputs};
  const encoded = JSON.stringify(data);
  if (state.reportedView === encoded) return;
  state.reportedView = encoded;
  state.socket.send(JSON.stringify({id: ++state.sequence, op: "view", data}));
  clearInterval(state.worksClock);
  state.worksClock = works ? setInterval(updateWorkTimers, 1000) : null;
}

function stopSocket() {
  reportView(null);
  const socket = state.socket;
  state.socket = null;
  state.ready = false;
  state.reportedView = null;
  clearInterval(state.worksClock);
  state.worksClock = null;
  socket?.close();
  rejectRequests(new Error("Connection interrupted. Retry the send to check delivery."));
  updateComposer();
  updatePush();
  updateOffice();
}

function sessionEnded(message) {
  state.active = false;
  state.connected = false;
  clearTimeout(state.retryTimer);
  stopSocket();
  $("workspace").hidden = true;
  $("works-page").hidden = true;
  $("quota-page").hidden = true;
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
        state.activities.clear();
        applyOffice(message.office);
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
      } else if (message.event === "office") {
        applyOffice(message.data);
      } else if (message.event === "works") {
        state.serverOffset = message.server_time * 1000 - Date.now();
        applyWorks(message);
      } else if (message.event === "quota") {
        renderQuota(message.data);
      } else if (message.event === "activity") {
        const {name, at} = message.data;
        state.activities.set(name, at);
        for (const node of $("works-list").querySelectorAll("[data-quiet-agent]")) {
          if (node.dataset.quietAgent === name) node.dataset.instant = Math.max(Number(node.dataset.started), at);
        }
        updateWorkTimers();
      } else if (message.event === "work_output") {
        state.serverOffset = message.server_time * 1000 - Date.now();
        renderWorkOutput(message.work_id, message.data);
        updateWorkTimers();
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
      state.reportedView = null;
      clearInterval(state.worksClock);
      state.worksClock = null;
      rejectRequests(error);
      updateComposer();
      updatePush();
      updateOffice();
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
      if (state.targetPeer !== null) {
        $("works-page").hidden = true;
        $("quota-page").hidden = true;
      }
      $("workspace").hidden = !($("works-page").hidden && $("quota-page").hidden);
      updateOffice();
      reportView();
      $("connection").textContent = "Connected";
      $("error").hidden = true;
      const peer = state.pending?.peer ?? state.targetPeer ?? state.peer ??
        state.conversations.find((item) => item.kind === "director")?.name ?? "all";
      state.targetPeer = null;
      if (!$("workspace").hidden) {
        if (peer !== state.peer || !state.messages.length) await openConversation(peer);
        else await syncMessages();
      }
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

function showPage(page) {
  $("workspace").hidden = page !== "chat";
  $("works-page").hidden = page !== "works";
  $("quota-page").hidden = page !== "quota";
  $("menu-dialog").close();
  reportView();
}

function applyOffice(data) {
  state.office = data;
  updateOffice();
}

function updateOffice() {
  $("nav-chat").disabled = $("nav-works").disabled = $("nav-quota").disabled = !state.ready;
  $("office-controls").hidden = !state.connected || !state.office;
  const disabled = !state.ready || state.officeBusy;
  $("office-toggle").disabled = disabled;
  $("office-timer-cancel").disabled = disabled;
  const input = $("office-timer-at");
  $("office-timer-set").disabled = disabled || !input.value || !input.validity.valid;
  for (const button of document.querySelectorAll("button[data-work-action]")) {
    button.disabled = !state.ready || state.workBusy.has(button.dataset.workAction);
  }
  if (!state.office) return;
  const {running, timer, finishing, zone} = state.office;
  for (const id of ["office-status", "works-office-status"]) {
    $(id).textContent = running ? "Running" : "Paused";
    $(id).classList.toggle("paused", !running);
  }
  $("office-toggle").textContent = running ? "Pause" : "Run";
  $("office-toggle").dataset.to = running ? "pause" : "run";
  $("office-finishing").hidden = !finishing;
  $("office-finishing").textContent = `${finishing} turn${finishing === 1 ? "" : "s"} still finishing`;
  $("office-zone").textContent = `Timer uses office computer time (UTC${zone}).`;
  if (!input.value) {
    $("office-timer-form").dataset.to = running ? "pause" : "run";
    $("office-timer-label").textContent = running ? "Pause at" : "Run at";
  }
  $("office-timer-pending").hidden = !timer;
  $("office-timer-description").textContent = timer ? `${timer.to === "run" ? "Run" : "Pause"} at ${timer.at}` : "";
}

async function officeAction(op, data = {}) {
  state.officeBusy = true;
  $("office-error").hidden = true;
  updateOffice();
  try {
    const office = await request(op, data);
    if (op === "timer") $("office-timer-at").value = "";
    applyOffice(office);
  } catch (error) {
    $("office-error").textContent = error.message;
    $("office-error").hidden = false;
  } finally {
    state.officeBusy = false;
    updateOffice();
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function dateLabel(date) {
  return date.toLocaleString("en", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}

function duration(seconds) {
  seconds = Math.floor(seconds);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function timerLabel(label, instant) {
  const node = element("span", "");
  node.dataset.instant = instant;
  node.dataset.label = label;
  return node;
}

function updateWorkTimers() {
  const now = (Date.now() + state.serverOffset) / 1000;
  for (const node of $("works-list").querySelectorAll("[data-instant]")) {
    node.textContent = node.dataset.label + duration(Math.max(0, now - Number(node.dataset.instant)));
  }
}

function mergeFields(target, patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (value !== null && !Array.isArray(value) && typeof value === "object" && target[key] !== null && typeof target[key] === "object") {
      mergeFields(target[key], value);
    } else target[key] = value;
  }
}

function applyWorks(message) {
  const data = message.data;
  if (message.kind === "snapshot") {
    state.works = data;
    for (const work of data.works) if (work.turn) state.activities.set(work.agent_name, Math.max(state.activities.get(work.agent_name) ?? work.turn.last_activity_wall_at, work.turn.last_activity_wall_at));
  }
  else {
    if (data.works) {
      const cards = new Map(state.works.works.map((work) => [work.id, work]));
      for (const work of data.works.added ?? []) cards.set(work.id, work);
      for (const patch of data.works.changed ?? []) mergeFields(cards.get(patch.id), patch);
      for (const id of data.works.removed ?? []) cards.delete(id);
      state.works.works = data.works.order ? data.works.order.map((id) => cards.get(id)) : [...cards.values()];
    }
    for (const key of ["wakes", "project_state"]) {
      if (key in data) state.works[key] = data[key];
    }
  }
  renderWorks();
}

function workButton(label, key, op, data) {
  const button = element("button", "danger", label);
  button.type = "button";
  button.dataset.workAction = key;
  button.disabled = !state.ready || state.workBusy.has(key);
  button.addEventListener("click", async () => {
    state.workBusy.add(key);
    updateOffice();
    try { await request(op, data); }
    catch (error) { showError(error); }
    finally { state.workBusy.delete(key); updateOffice(); }
  });
  return button;
}

function renderWorks() {
  const snapshot = state.works;
  const previous = new Map([...$("works-list").children].map((card) => [Number(card.dataset.work), card]));
  const outputScroll = new Map([...$("works-list").querySelectorAll("[data-output] .work-tail")]
    .map((output) => [output, output.scrollTop]));
  const fragment = document.createDocumentFragment();
  if (snapshot.project_state !== "ready") {
    fragment.append(element("p", "empty", `Project setup: ${snapshot.project_state}. Set it up on the computer.`));
  } else if (!snapshot.works.length) {
    fragment.append(element("p", "empty", "No open works."));
  } else for (const work of snapshot.works) {
    const card = element("article", "work-card");
    card.dataset.work = work.id;
    const header = element("header", "");
    header.append(element("h2", "", work.agent_name),
      element("span", `work-status ${work.derived_status}`, work.derived_status));
    card.append(header, element("p", "work-meta", [
      `${work.agent_runtime}/${work.agent_model}`, work.agent_effort, work.log_role, work.log_complexity,
    ].filter(Boolean).join(" · ")));
    if (work.task_title) card.append(element("p", "work-task", work.task_title));
    card.append(element("code", "", work.branch));
    card.append(element("p", "work-meta", `Started ${dateLabel(new Date(work.started_at))}`));
    const turn = work.turn;
    if (turn) {
      const live = element("div", "work-live");
      const quiet = timerLabel("Quiet ", Math.max(turn.started_wall_at, state.activities.get(work.agent_name) ?? turn.started_wall_at));
      quiet.dataset.quietAgent = work.agent_name;
      quiet.dataset.started = turn.started_wall_at;
      const previousStop = previous.get(work.id)?.querySelector("button[data-work-action]");
      const stop = previousStop?.dataset.workAction === `stop:${work.agent_name}` ? previousStop :
        workButton("Stop turn", `stop:${work.agent_name}`, "work_stop", {agent: work.agent_name});
      live.append(timerLabel("Running ", turn.started_wall_at), quiet,
        stop);
      card.append(live);
      for (const {id, summary, started_at} of turn.call_instants) {
        const call = element("div", "work-call");
        call.dataset.call = id;
        call.append(element("span", "", summary || "Tool call"), document.createTextNode(" · "), timerLabel("", started_at));
        card.append(call);
      }
    } else if (work.derived_status === "reported") {
      card.append(element("p", "page-note", `Waiting on ${work.assigner_name} to close it.`));
    } else if (work.derived_status === "waiting") {
      card.append(element("p", "page-note", `No turn. ${work.agent_name} is ${work.agent_status}.`));
    }
    const used = turn?.context_used ?? work.context_used;
    const limit = turn?.context_used != null ? turn.context_limit : work.context_limit;
    const context = element("div", "work-context", used == null || limit == null ? "Context fill: unknown" :
      `Context fill: ${Math.floor(100 * used / limit)}% · ${used.toLocaleString("en")} / ${limit.toLocaleString("en")}`);
    if (used != null && limit != null) {
      const bar = element("progress", "");
      bar.max = limit;
      bar.value = used;
      bar.setAttribute("aria-label", "Context fill");
      context.append(bar);
    }
    card.append(context);
    const brief = element("details", "");
    brief.dataset.brief = "";
    brief.open = previous.get(work.id)?.querySelector("[data-brief]").open ?? false;
    brief.append(element("summary", "", "Brief"), element("div", "work-brief", work.brief));
    card.append(brief);
    if (work.status === "paused" && work.pause_reason) {
      card.append(element("p", "page-note", work.pause_reason.replaceAll("_", " ")));
      if (work.resume_after) card.append(element("p", "page-note", `Earliest resume ${dateLabel(new Date(Number(work.resume_after) * 1000))}`));
    }
    if (work.status === "failed" && work.fail_reason) card.append(element("p", "control-error", work.fail_reason.replaceAll("_", " ")));
    let output = previous.get(work.id)?.querySelector("[data-output]");
    if (!output) {
      output = element("details", "");
      output.dataset.output = work.id;
      output.append(element("summary", "", "Output"), element("div", "work-tail", "Loading output…"));
      output.addEventListener("toggle", () => reportView());
    }
    card.append(output);
    fragment.append(card);
  }
  $("works-list").replaceChildren(fragment);
  for (const [output, top] of outputScroll) output.scrollTop = top;
  const wakes = document.createDocumentFragment();
  for (const wake of snapshot.wakes) {
    const row = element("div", "wake-item");
    const description = element("div", "");
    description.append(element("strong", "", `${wake.sender} → ${wake.recipient}`),
      element("p", "work-meta", dateLabel(new Date(wake.due_at))), element("p", "", wake.body));
    row.append(description, workButton("Cancel", `wake:${wake.id}`, "wake_cancel", {wake_id: wake.id}));
    wakes.append(row);
  }
  $("works-wakes").replaceChildren(wakes);
  $("wakes-section").hidden = !snapshot.wakes.length;
  updateWorkTimers();
  reportView();
}

function renderQuota(data) {
  const quota = document.createDocumentFragment();
  for (const [runtime, buckets] of data) {
    if (!buckets.length) quota.append(element("p", "page-note", `${runtime}: no data`));
    for (const bucket of buckets) {
      const item = element("div", "quota-item");
      item.append(element("strong", "", `${runtime} · ${bucket.label} · ${Math.floor(bucket.remaining_fraction * 100)}%`));
      const bar = element("progress", "");
      bar.max = 1;
      bar.value = bucket.remaining_fraction;
      bar.setAttribute("aria-label", `${runtime} ${bucket.label} quota left`);
      item.append(bar, element("small", "", bucket.reset_time ?
        `Resets ${dateLabel(new Date(Number(bucket.reset_time) * 1000))}` : "Reset time unknown"));
      quota.append(item);
    }
  }
  $("quota-list").replaceChildren(quota);
}

function renderWorkOutput(workId, data) {
  const output = $("works-list").querySelector(`[data-output="${workId}"] .work-tail`);
  if (output) officeRenderTranscript(output, data, "page-note");
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
  showPage("chat");
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
$("menu-button").addEventListener("click", () => {
  $("menu-dialog").showModal();
  reportView();
});
$("close-menu").addEventListener("click", () => $("menu-dialog").close());
$("menu-dialog").addEventListener("close", () => reportView());
$("nav-chat").addEventListener("click", () => { showPage("chat"); syncMessages().catch(showError); });
$("nav-works").addEventListener("click", () => showPage("works"));
$("nav-quota").addEventListener("click", () => showPage("quota"));
$("office-toggle").addEventListener("click", () => officeAction("switch", {to: $("office-toggle").dataset.to}));
$("office-timer-at").addEventListener("input", updateOffice);
$("office-timer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  officeAction("timer", {to: event.currentTarget.dataset.to, at: $("office-timer-at").value});
});
$("office-timer-cancel").addEventListener("click", () => officeAction("timer_cancel"));
$("device-button").addEventListener("click", () => {
  $("menu-dialog").close();
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
