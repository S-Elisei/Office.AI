// Desktop cursors, event predicates and recorded event clocks.
(function () {
  const owner = document.body.dataset.owner;
  const feedState = new WeakMap();
  let serverOffset = 0;
  const activityInstants = new Map();

  function messageMatches(kind, peer, payload) {
    if (!["chat", "dm"].includes(kind)) return false;
    if (payload.subject_type !== "message") return false;
    if (kind === "chat") return payload.channel === "all" && payload.recipient === null;
    return payload.channel === "dm" && (peer ?
      (payload.sender === owner && payload.recipient === peer || payload.sender === peer && payload.recipient === owner) :
      payload.sender === owner || payload.recipient === owner);
  }

  window.officeConcerns = function (region, event) {
    const payload = event.detail;
    const kind = event.type.slice(7);
    const id = region.id;
    if (kind === "messages") {
      if (id === "monitor-wakes") return payload.subject_type === "scheduled_message";
      const name = region.dataset.counter;
      if (id === "dm-threads" || name === "dm") return messageMatches("dm", null, payload);
      if (name === "chat") return messageMatches("chat", null, payload);
      if (id === "main-chat") return messageMatches("dm", region.querySelector("[data-office-seen-peer]")?.dataset.officeSeenPeer, payload);
      return false;
    }
    if (kind === "tickets") {
      if (id === "ticket-detail") return payload.subject_type === "ticket" && payload.subject_id === Number(region.dataset.ticketId);
      return payload.subject_type === "ticket";
    }
    if (kind === "tasks" && id === "kanban-crumbs") return region.querySelector("[data-chain-ids]").dataset.chainIds.split(",").includes(String(payload.subject_id));
    if (kind === "tasks" && id === "kanban-board") {
      const board = region.querySelector("[data-watched-tasks]");
      if (board.dataset.flat === "1") return true;
      const ids = board.dataset.watchedTasks.split(",");
      const parent = Number(board.dataset.parent);
      return ids.includes(String(payload.subject_id)) || ids.includes(String(payload.parent_task_id)) || ids.includes(String(payload.previous_parent_task_id)) ||
        Number(payload.parent_task_id) === parent || Number(payload.previous_parent_task_id) === parent;
    }
    if (kind === "notices" && id === "main-plate") return payload.severity === "critical" || payload.dismissed !== undefined;
    if (kind === "agents" && ["main-stop", "main-context"].includes(id)) {
      return payload.subject_type === "agent" && payload.subject_id === Number(document.querySelector("[data-director-id]")?.dataset.directorId) &&
        (id !== "main-stop" || payload.status !== undefined);
    }
    if (kind === "agents" && id === "office-switch") return ["running", "idle"].includes(payload.status);
    if (kind === "prs" && region.dataset.counter === "prs") return payload.subject_type === "pr";
    if (kind === "settings" && id === "office-switch") return payload.key === "switch_timer";
    if (kind === "activity") return id === "main-activity" && payload.subject_type === "agent" && payload.name === document.querySelector("[data-director-name]")?.dataset.directorName;
    if (kind === "wiki" && id === "knowledge-wiki") return !region.dataset.path || payload.path === region.dataset.path;
    return true;
  };

  function node(html) {
    const template = document.createElement("template");
    template.innerHTML = html;
    return template.content.firstElementChild;
  }

  function atFoot(box) { return box.scrollHeight - box.scrollTop - box.clientHeight < 24; }
  window.officeAtFoot = atFoot;

  async function pull(feed, older = false) {
    let state = feedState.get(feed);
    if (!state) { state = {busy: false, pending: false, older: false}; feedState.set(feed, state); }
    if (state.busy) { if (older) state.older = true; else state.pending = true; return; }
    state.busy = true;
    try {
      do {
        if (!older) state.pending = false;
        const first = feed.querySelector("[data-row-id]");
        const url = new URL(feed.dataset.officeFeed, location.origin);
        url.searchParams.set(older ? "before" : "after", older ? first.dataset.rowId : feed.dataset.lastId);
        const response = await fetch(url);
        const data = await response.json();
        const stick = atFoot(feed), top = feed.scrollTop, height = feed.scrollHeight;
        const fragment = document.createDocumentFragment();
        for (const row of data.rows) {
          if (!feed.querySelector('[data-row-id="' + row.id + '"]')) fragment.append(node(row.html));
          feed.dataset.lastId = Math.max(Number(feed.dataset.lastId), row.id);
        }
        if (older) feed.prepend(fragment); else feed.append(fragment);
        feed.querySelector("[data-feed-empty]").hidden = Boolean(feed.querySelector("[data-row-id]"));
        if (older) {
          feed.scrollTop = top + feed.scrollHeight - height;
          const region = feed.parentElement;
          region.querySelector("[data-office-older]").hidden = !data.more;
          region.querySelector("[data-office-latest]").hidden = false;
        } else feed.scrollTop = stick ? feed.scrollHeight : top;
        if (feed.dataset.officeSeenChannel) feed.dataset.officeSeenId = feed.dataset.lastId;
        if (!older && data.more) state.pending = true;
        window.officeReportSeen?.();
      } while (state.pending && !older);
    } finally { state.busy = false; }
    if (state.older) { state.older = false; pull(feed, true); }
    else if (state.pending) pull(feed);
  }

  function feeds() { return document.querySelectorAll("[data-office-feed]"); }
  document.body.addEventListener("office:messages", (event) => {
    for (const feed of feeds()) {
      const [, kind, peer] = new URL(feed.dataset.officeFeed, location.origin).pathname.split("/").slice(2);
      if (messageMatches(kind, decodeURIComponent(peer), event.detail)) pull(feed);
    }
  });
  for (const kind of ["ticket", "pr"]) {
    document.body.addEventListener("office:" + (kind === "ticket" ? "tickets" : "prs"), (event) => {
      if (event.detail.subject_type !== kind + "_comment") return;
      for (const feed of feeds()) if (feed.dataset.officeFeed === "/live/thread/" + kind + "/" + event.detail.subject_id) pull(feed);
    });
  }
  document.addEventListener("click", (event) => {
    const older = event.target.closest("[data-office-older]");
    const latest = event.target.closest("[data-office-latest]");
    if (older) pull(document.getElementById(older.dataset.officeOlder).querySelector("[data-office-feed]"), true);
    if (latest) {
      const feed = document.getElementById(latest.dataset.officeLatest).querySelector("[data-office-feed]");
      feed.scrollTop = feed.scrollHeight;
    }
  });

  let prsBusy = false, prsPending = false;
  async function refreshPrs() {
    const region = document.querySelector("[data-office-prs]");
    if (!region) return;
    if (prsBusy) { prsPending = true; return; }
    prsBusy = true;
    try {
      do {
        prsPending = false;
        const known = [...region.querySelectorAll("[data-pr-id]")].map(card => card.dataset.prId);
        const response = await fetch("/live/prs?known=" + known.join(","));
        const data = await response.json();
        const ids = new Set(data.prs.map(pr => String(pr.id)));
        for (const card of region.querySelectorAll("[data-pr-id]")) if (!ids.has(card.dataset.prId)) card.remove();
        for (const pr of [...data.prs].reverse()) {
          let card = region.querySelector('[data-pr-id="' + pr.id + '"]');
          if (!card) { card = node(pr.shell); region.prepend(card); htmx.process(card); pull(card.querySelector("[data-office-feed]")); }
          const display = card.querySelector("[data-pr-display]");
          if (display.dataset.html !== pr.display) { display.innerHTML = pr.display; display.dataset.html = pr.display; }
        }
        region.querySelector("[data-pr-empty]").hidden = data.prs.length !== 0;
        document.dispatchEvent(new CustomEvent("office:forms-added"));
      } while (prsPending);
    } finally { prsBusy = false; }
  }
  document.body.addEventListener("office:prs", refreshPrs);

  const submitted = new WeakMap();
  document.addEventListener("htmx:beforeRequest", event => {
    const form = event.detail.elt;
    if (form.matches("form")) submitted.set(form, [...form.querySelectorAll("input, textarea")].map(field => [field, field.value]));
  });
  document.addEventListener("htmx:afterRequest", event => {
    const form = event.detail.elt;
    if (!form.matches("form") || event.detail.xhr.status < 200 || event.detail.xhr.status >= 300) return;
    if (form.dataset.officeSend !== undefined || form.dataset.officeCreate !== undefined || form.dataset.officeTimer !== undefined) {
      for (const [field, value] of submitted.get(form)) if (field.value === value && field.type !== "hidden") field.value = field.defaultValue;
      form.dispatchEvent(new Event("input", {bubbles:true}));
      if (form.hasAttribute("data-office-timer")) syncSwitch();
    }
    if (form.dataset.officeSend !== undefined) {
      const region = document.getElementById(form.dataset.officeSend);
      const feed = (region || form.parentElement).querySelector("[data-office-feed]");
      if (feed) pull(feed);
    }
  });

  function syncSwitch() {
    const live = document.getElementById("office-switch");
    const form = document.getElementById("office-timer-form");
    if (!form.elements.at.value) {
      const running = live.dataset.running === "1";
      form.elements.to.value = running ? "pause" : "run";
      form.querySelector("label").textContent = running ? "Pause at" : "Run at";
    }
  }
  function syncTicket() {
    const status = document.querySelector("[data-ticket-status]");
    if (status?.dataset.ticketStatus === "resolved") for (const form of document.querySelectorAll("[data-ticket-form]")) form.closest(".resolve-panel, .resolve-panel-override").remove();
  }
  document.addEventListener("htmx:afterSwap", () => { syncSwitch(); syncTicket(); tick(); });

  function duration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + seconds % 60 + "s";
    return Math.floor(seconds / 3600) + "h " + Math.floor(seconds % 3600 / 60) + "m";
  }
  function tick() {
    const now = (Date.now() + serverOffset) / 1000;
    for (const element of document.querySelectorAll("[data-office-instant]")) {
      if (element.hasAttribute("data-office-quiet")) {
        element.dataset.officeInstant = Math.max(Number(element.dataset.officeInstant), activityInstants.get(element.dataset.clockKey) ?? Number(element.dataset.officeInstant));
      }
      const age = now - Number(element.dataset.officeInstant);
      element.textContent = (element.dataset.prefix || "") + duration(age);
      if (element.hasAttribute("data-office-quiet")) element.className = age >= 300 ? "badge warn" : "text-faint";
    }
  }
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", tick);
  function recordActivity(kind, name, at) {
    const key = kind + ":" + name;
    activityInstants.set(key, Math.max(activityInstants.get(key) ?? at, at));
  }
  document.body.addEventListener("office:activity", event => {
    const payload = event.detail;
    if (document.getElementById("monitor-list") || [...document.querySelectorAll("[data-clock-key]")].some(element => element.dataset.clockKey === payload.subject_type + ":" + payload.name)) {
      recordActivity(payload.subject_type, payload.name, payload.at);
      tick();
    }
  });

  const works = document.getElementById("monitor-list");
  let socket;
  function reportOutputs() {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({outputs: [...works.querySelectorAll("details[data-output][open]")].map(panel => Number(panel.dataset.output))}));
  }
  function setField(target, html) {
    const open = new Set([...target.querySelectorAll("details[open]")].map(details => details.dataset.officeDetails));
    target.innerHTML = html;
    for (const details of target.querySelectorAll("details")) if (open.has(details.dataset.officeDetails)) details.open = true;
    htmx.process(target);
  }
  function applyOutput(id, data) {
    const output = works.querySelector('[data-output="' + id + '"] .tail-view');
    if (output) officeRenderTranscript(output, data, "empty");
  }
  function connectWorks() {
    socket = new WebSocket(location.origin.replace(/^http/, "ws") + "/live/works");
    const connection = socket;
    let initialized = false;
    socket.onopen = reportOutputs;
    socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.event === "activity") {
        recordActivity(message.data.subject_type, message.data.name, message.data.at);
        tick();
        return;
      }
      serverOffset = message.server_time * 1000 - Date.now();
      if (message.event === "output") { applyOutput(message.id, message.data); return; }
      const data=message.data;
      for (const [name, at] of Object.entries(data.activity ?? {})) recordActivity("agent", name, at);
      if (!initialized) {
        const ids = new Set((data.added ?? []).map(work => String(work.id)));
        for (const card of works.querySelectorAll("[data-work-id]")) if (!ids.has(card.dataset.workId)) card.remove();
        works.querySelector("[data-works-empty]")?.remove(); initialized=true;
      }
      for (const id of data.removed ?? []) works.querySelector('[data-work-id="'+id+'"]').remove();
      for (const patch of [...data.added ?? [], ...data.changed ?? []]) {
        let card=works.querySelector('[data-work-id="'+patch.id+'"]');
        if (!card) {
          card=document.createElement("article"); card.className="panel work-card"; card.dataset.workId=patch.id;
          for (const field of ["head","live","context","brief","reason","calls"]) {const part=document.createElement("div");part.dataset.workField=field;card.append(part);}
          const output=node('<details class="work-output" data-output="'+patch.id+'"><summary>Output</summary><div class="hint">The current turn, last 200 lines.</div><div class="tail-view"></div></details>');
          output.addEventListener("toggle", reportOutputs); card.append(output); works.append(card);
        }
        for (const [field,html] of Object.entries(patch.fields)) setField(card.querySelector('[data-work-field="'+field+'"]'),html);
      }
      if (data.order) {
        for (let i=0; i<data.order.length; i++) { const card=works.querySelector('[data-work-id="'+data.order[i]+'"]'); if (works.children[i]!==card) works.insertBefore(card,works.children[i] || null); }
        if (!data.order.length) works.replaceChildren();
      }
      if (!works.querySelector("[data-work-id]")) {
        let empty=works.querySelector("[data-works-empty]");
        if (!empty) {empty=document.createElement("div");empty.className="empty";empty.dataset.worksEmpty="";works.append(empty);}
        empty.textContent = data.project_state && data.project_state!=="ready" ? "Set up the project on Main." : "No open works.";
      }
      tick();
    };
    socket.onclose = () => {if (socket===connection) setTimeout(connectWorks,3000);};
  }

  window.officeRefreshLive = function () { for (const feed of feeds()) pull(feed); refreshPrs(); };
  syncSwitch(); syncTicket(); tick();
  if (works) connectWorks();
})();
