// Office.AI — SSE-to-HTMX bridge.

(function () {
  // Must match the kinds office/core.py emits.
  const KINDS = [
    "agents", "works", "tasks", "messages", "prs", "quota", "wiki", "rules", "settings",
    "tickets", "notices", "stages",
  ];

  document.addEventListener("htmx:beforeSwap", function (evt) {
    const xhr = evt.detail.xhr;
    const status = xhr && xhr.status;
    if (status === 400) {
      evt.detail.shouldSwap = true;
      evt.detail.isError = false; // an expected refusal, not a transport failure
    } else if (status === 409) {
      evt.detail.shouldSwap = false;
      evt.detail.isError = false;
      window.alert(xhr.responseText);
    }
  });

  let streamWasDown = false;

  function connect() {
    const source = new EventSource("/events/stream");

    for (const kind of KINDS) {
      source.addEventListener(kind, (evt) => {
        let payload = null;
        try {
          payload = JSON.parse(evt.data);
        } catch (err) {
        }
        document.body.dispatchEvent(new CustomEvent("office:" + kind, { detail: payload }));
      });
    }

    source.addEventListener("open", () => {
      setConnIndicator(true);
      if (streamWasDown) refreshCleanRegions();
      streamWasDown = false;
    });
    source.addEventListener("error", () => {
      setConnIndicator(false);
      streamWasDown = true;
    });
  }

  function setConnIndicator(live) {
    const dot = document.querySelector("[data-conn-dot]");
    const label = document.querySelector("[data-conn-label]");
    if (dot) dot.classList.toggle("live", live);
    if (label) label.textContent = live ? "live" : "reconnecting…";
  }

  const FOOT_SLACK_PX = 24;

  function atFoot(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < FOOT_SLACK_PX;
  }

  function isDirty(region) {
    const fields = region.querySelectorAll('input[type="text"], textarea');
    for (const field of fields) {
      const details = field.closest("details");
      if (details && !details.open) continue;
      if (field.value !== field.defaultValue) return true;
    }
    return false;
  }

  // Referenced by name from hx-trigger's bracket filter. Must be a global.
  window.officeShouldSkip = function (region) {
    if (isDirty(region)) {
      region.dataset.officePending = "1";
      return true;
    }
    return false;
  };

  // Re-requests one region's fragment.
  function refreshRegion(region) {
    if (!window.htmx) return;
    const swap = region.getAttribute("hx-swap") || "innerHTML";
    window.htmx.ajax("GET", region.getAttribute("hx-get"), { source: region, target: region, swap: swap });
  }

  // Every region that refreshes from the stream: an hx-get element listening for an "office:" event.
  function refreshCleanRegions() {
    const regions = document.querySelectorAll('[hx-get][hx-trigger*="office:"]');
    for (const region of regions) {
      if (window.officeShouldSkip(region)) continue;
      refreshRegion(region);
    }
  }

  // ---- collapsed blocks, and the output tails that live inside them --------
  const detailsOpen = new Map();

  function loadLazy(details) {
    const lazy = details.querySelector("[data-office-lazy]");
    if (lazy && details.open) refreshRegion(lazy);
  }

  document.addEventListener(
    "toggle",
    (e) => {
      const el = e.target;
      if (!el.dataset || el.dataset.officeDetails === undefined) return;
      detailsOpen.set(el.dataset.officeDetails, el.open);
      if (el.open) loadLazy(el);
    },
    true
  );

  // ---- keeping the reader's place across a swap ---------------------------
  const SCROLL_BOXES =
    "[data-office-lazy], .thread[data-office-stick], .kanban-scroll, .kanban-cards, #main-chat";
  const scrollPlace = new Map();

  function scrollKey(box) {
    if (box.id) return box.id;
    const region = box.closest("[hx-get]");
    return `${(region && region.id) || ""}:${box.className}`;
  }

  function rememberScroll(box) {
    scrollPlace.set(scrollKey(box), {
      top: box.scrollTop,
      left: box.scrollLeft,
      atFoot: atFoot(box),
    });
  }

  function rememberScrollIn(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.matches && root.matches(SCROLL_BOXES)) rememberScroll(root);
    for (const box of root.querySelectorAll(SCROLL_BOXES)) rememberScroll(box);
  }

  // data-office-stick is read off the box or off the conversation inside it.
  function stickOf(box) {
    if (box.dataset.officeStick) return box.dataset.officeStick;
    const thread = box.querySelector(".thread[data-office-stick]");
    return thread ? thread.dataset.officeStick : "";
  }

  function placeScroll(box) {
    const follows = box.matches("[data-office-lazy]") || stickOf(box) === "newest";
    const was = scrollPlace.get(scrollKey(box));
    if (!was) {
      if (follows) box.scrollTop = box.scrollHeight;
      return;
    }
    if (follows && was.atFoot) box.scrollTop = box.scrollHeight;
    else box.scrollTop = was.top;
    box.scrollLeft = was.left;
  }

  function placeScrollIn(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.matches && root.matches(SCROLL_BOXES)) placeScroll(root);
    for (const box of root.querySelectorAll(SCROLL_BOXES)) placeScroll(box);
  }

  document.addEventListener(
    "scroll",
    (event) => {
      const box = event.target;
      if (box && box.matches && box.matches(SCROLL_BOXES)) rememberScroll(box);
    },
    { capture: true, passive: true }
  );

  document.addEventListener("htmx:beforeSwap", (e) => {
    rememberScrollIn(e.detail && e.detail.target);
  });

  document.addEventListener("htmx:afterSwap", (e) => {
    const root = e.target;
    placeScrollIn(document);
    reportSeen();
    if (!root || !root.querySelectorAll) return;
    for (const el of root.querySelectorAll("details[data-office-details]")) {
      if (detailsOpen.get(el.dataset.officeDetails) && !el.open) el.open = true;
    }
  });

  // ---- what the owner has actually read ----------------------------------
  const reported = new Map();
  const retryFloor = new Map();
  const RETRY_FLOOR_MS = 5000;

  const IDLE_AFTER_MS = 10 * 60 * 1000;
  let lastInputAt = Date.now();

  function noteInput() {
    const wasIdle = Date.now() - lastInputAt > IDLE_AFTER_MS;
    lastInputAt = Date.now();
    if (wasIdle) reportSeen();
  }

  for (const kind of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"]) {
    document.addEventListener(kind, noteInput, { capture: true, passive: true });
  }

  function reportSeen() {
    if (document.visibilityState !== "visible" || !document.hasFocus()) return;
    if (Date.now() - lastInputAt > IDLE_AFTER_MS) return;
    for (const box of document.querySelectorAll(".thread[data-office-seen-id]")) {
      const channel = box.dataset.officeSeenChannel;
      const id = box.dataset.officeSeenId;
      const peer = box.dataset.officeSeenPeer || "";
      if (!channel || !id) continue;
      const key = channel + " " + peer;
      if (reported.get(key) === id) continue;
      if (Date.now() < (retryFloor.get(key) || 0)) continue;
      if (!atFoot(box)) continue;
      reported.set(key, id);
      const forget = () => {
        if (reported.get(key) === id) reported.delete(key);
        retryFloor.set(key, Date.now() + RETRY_FLOOR_MS);
      };
      fetch("/messages/seen", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ channel: channel, peer: peer, message_id: id }),
      }).then((response) => {
        if (response.ok) {
          retryFloor.delete(key);
          refreshCounters();
        }
        else if (response.status >= 500) forget();
      }, forget);
    }
  }

  function refreshCounters() {
    for (const counter of document.querySelectorAll(".nav-count[hx-get]")) refreshRegion(counter);
  }

  document.addEventListener("visibilitychange", reportSeen);
  window.addEventListener("focus", reportSeen);
  document.addEventListener("scroll", reportSeen, { capture: true, passive: true });

  function catchUpIfClean(startEl) {
    const region = startEl.closest && startEl.closest("[hx-get]");
    if (!region || region.dataset.officePending !== "1") return;
    if (isDirty(region)) return;
    delete region.dataset.officePending;
    refreshRegion(region);
  }

  document.addEventListener("input", (e) => catchUpIfClean(e.target));
  document.addEventListener(
    "toggle",
    (e) => {
      if (!e.target.open) catchUpIfClean(e.target);
    },
    true
  );

  // ---- the notice area ----------------------------------------------------
  const NOTICE_FADE_MS = 5000;

  function armNoticeFades() {
    for (const notice of document.querySelectorAll('[data-office-notice="ok"]')) {
      if (notice.dataset.officeNoticeArmed) continue;
      notice.dataset.officeNoticeArmed = "1";
      setTimeout(() => notice.remove(), NOTICE_FADE_MS);
    }
  }

  document.addEventListener("htmx:afterSwap", armNoticeFades);

  document.addEventListener("click", (e) => {
    const button = e.target.closest && e.target.closest("[data-office-notice-close]");
    if (!button) return;
    const notice = button.closest("[data-office-notice]");
    if (notice) notice.remove();
  });

  // data-office-required names the fields that must have something in them before the form can be sent.
  function syncRequired(form) {
    if (!form || !form.dataset || !form.dataset.officeRequired) return;
    const names = form.dataset.officeRequired.split(/\s+/).filter(Boolean);
    const ready = names.every((name) => {
      const field = form.elements[name];
      return field && typeof field.value === "string" && field.value.trim() !== "";
    });
    for (const button of form.querySelectorAll("button[type=submit]")) {
      button.disabled = !ready;
    }
  }

  function syncAllRequired(root) {
    for (const form of root.querySelectorAll("form[data-office-required]")) syncRequired(form);
  }

  document.addEventListener("input", (e) => syncRequired(e.target.form));
  document.addEventListener("htmx:afterSwap", () => syncAllRequired(document));

  if (document.body) {
    connect();
    placeScrollIn(document);
    reportSeen();
    syncAllRequired(document);
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      connect();
      placeScrollIn(document);
      reportSeen();
      syncAllRequired(document);
    });
  }
})();
