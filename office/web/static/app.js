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

  // One line in the office log about this page's connection.
  function report(what) {
    navigator.sendBeacon("/events/report", new URLSearchParams({ page: location.pathname, what: what }));
  }

  function secondsSince(time) {
    return Math.round((Date.now() - time) / 1000);
  }

  function pageState() {
    return `${document.visibilityState}, ${navigator.onLine ? "online" : "offline"}`;
  }

  let streamWasDown = false;
  let openedAt = 0;
  let downSince = 0;

  function connect() {
    const source = new EventSource("/events/stream");
    openedAt = Date.now();

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
      if (streamWasDown) {
        report(`stream back after ${secondsSince(downSince)}s down`);
        refreshCleanRegions();
      }
      streamWasDown = false;
      openedAt = Date.now();
    });
    source.addEventListener("error", () => {
      setConnIndicator(false);
      const state = source.readyState === EventSource.CLOSED ? "closed for good" : "retrying";
      if (streamWasDown) {
        report(`stream reconnect failed after ${secondsSince(downSince)}s down: ${state}, ${pageState()}`);
      } else {
        downSince = Date.now();
        report(`stream lost after ${secondsSince(openedAt)}s open: ${state}, ${pageState()}`);
      }
      streamWasDown = true;
      // The browser does not retry a stream it closed; a new one takes its place.
      if (source.readyState === EventSource.CLOSED) setTimeout(connect, REOPEN_MS);
    });
  }

  const REOPEN_MS = 3000;

  document.addEventListener("htmx:sendError", (e) => {
    report(`${e.detail.requestConfig.verb.toUpperCase()} ${e.detail.pathInfo.requestPath} failed to send, ${pageState()}`);
  });
  document.addEventListener("htmx:responseError", (e) => {
    report(`${e.detail.requestConfig.verb.toUpperCase()} ${e.detail.pathInfo.requestPath} answered ${e.detail.xhr.status}`);
  });

  // A visible page whose one-second timer fires this much late was blocked or throttled.
  const STALL_MS = 3000;
  // Swaps slower than this are named in the stall report.
  const SLOW_SWAP_MS = 100;
  // The latest swaps: when each ended, its target and how long htmx's swap and the
  // afterSwap handlers registered before the one below took.
  const recentSwaps = [];
  let swapStart = 0;

  document.addEventListener("htmx:beforeSwap", () => {
    swapStart = performance.now();
  });

  function swapsDuring(ms) {
    const since = Date.now() - ms;
    return recentSwaps
      .filter((s) => s.at >= since && s.ms >= SLOW_SWAP_MS)
      .map((s) => `${s.target} ${Math.round(s.ms)}ms`);
  }

  function tailChars() {
    let chars = 0;
    for (const tail of document.querySelectorAll(".tail-view")) chars += tail.textContent.length;
    return chars;
  }

  let tickAt = Date.now();
  setInterval(() => {
    const late = Date.now() - tickAt - 1000;
    tickAt = Date.now();
    if (late > STALL_MS && document.visibilityState === "visible") {
      const slow = swapsDuring(late + 2000);
      report(
        `page stalled for ${Math.round(late / 1000)}s, ${document.hasFocus() ? "focused" : "unfocused"}; ` +
        `slow swaps: ${slow.length ? slow.join(", ") : "none"}; ` +
        `${document.getElementsByTagName("*").length} elements, ${tailChars()} chars in tails`
      );
    }
  }, 1000);
  document.addEventListener("visibilitychange", () => {
    tickAt = Date.now();
  });

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

  // A region is a draft while a field differs from its value or an editor's rows were
  // added, deleted or moved.
  function isDirty(region) {
    if (region.querySelector("[data-editor-changed]")) return true;
    const fields = region.querySelectorAll(
      'input[type="text"], input[type="time"], input[type="number"], input[type="range"], input[type="checkbox"], textarea'
    );
    for (const field of fields) {
      const details = field.closest("details");
      if (details && !details.open) continue;
      if (field.type === "checkbox") {
        if (field.checked !== field.defaultChecked) return true;
        continue;
      }
      if (field.value !== field.defaultValue || field.validity.badInput) return true;
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
    "[data-office-lazy], .thread[data-office-stick], .kanban-scroll, .kanban-cards, #main-chat, .editor-scroll";
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

  // The page's own scroll: a swapped region arrives with its details closed,
  // and the page is shorter until they reopen.
  let pageTop = 0;

  document.addEventListener("htmx:beforeSwap", (e) => {
    pageTop = document.scrollingElement.scrollTop;
    rememberScrollIn(e.detail && e.detail.target);
  });

  document.addEventListener("htmx:afterSwap", (e) => {
    const root = e.target;
    if (root && root.querySelectorAll) {
      for (const el of root.querySelectorAll("details[data-office-details]")) {
        if (detailsOpen.get(el.dataset.officeDetails) && !el.open) el.open = true;
      }
    }
    document.scrollingElement.scrollTop = pageTop;
    placeScrollIn(document);
    reportSeen();
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

  // data-office-required names the fields that must have something in them, and pass their own
  // constraints, before the form can be sent.
  function syncRequired(form) {
    if (!form || !form.dataset || !form.dataset.officeRequired) return;
    const names = form.dataset.officeRequired.split(/\s+/).filter(Boolean);
    const ready = names.every((name) => {
      const field = form.elements[name];
      return field && typeof field.value === "string" && field.value.trim() !== "" && field.checkValidity();
    });
    for (const button of form.querySelectorAll("button[type=submit]")) {
      button.disabled = !ready;
    }
  }

  function syncAllRequired(root) {
    for (const form of root.querySelectorAll("form[data-office-required]")) syncRequired(form);
  }

  // A wallet's reserve is the slider's distance from the right edge of its bar.
  function syncReserve(row) {
    const reserve = 100 - Number(row.querySelector("[data-reserve-slider]").value);
    const week = Number(row.querySelector("[data-week]").value) || 0;
    row.querySelector("[data-reserve]").value = reserve;
    row.querySelector("[data-reserve-label]").textContent =
      `${reserve}% reserved \u00b7 ${(reserve * week / 100).toFixed(1)} ${row.dataset.currency}`;
  }

  // ---- table editors --------------------------------------------------------
  document.addEventListener("click", (e) => {
    const button = e.target.closest && e.target.closest("[data-editor-act]");
    if (!button) return;
    const editor = button.closest("[data-editor]");
    const row = button.closest("tr");
    switch (button.dataset.editorAct) {
      case "add":
        editor.querySelector("tbody").append(
          editor.querySelector("[data-editor-blank]").content.cloneNode(true)
        );
        break;
      case "up":
        if (row.previousElementSibling) row.previousElementSibling.before(row);
        break;
      case "down":
        if (row.nextElementSibling) row.nextElementSibling.after(row);
        break;
      case "delete":
        row.remove();
        break;
    }
    editor.dataset.editorChanged = "1";
    validatePriceLists(editor.closest("form"));
  });

  // A works or own row priced above the 5h window of its model's runtime, and an own
  // row whose role, model and effort another own row has, cannot be saved. The form's
  // data-price-runtimes maps a model to its runtime and to the name of the field holding
  // that runtime's window.
  function validatePriceLists(form) {
    if (!form || !form.dataset.priceRuntimes) return;
    const runtimes = JSON.parse(form.dataset.priceRuntimes);
    for (const list of ["works", "own"]) {
      for (const price of form.querySelectorAll(`input[name="${list}_price"]`)) {
        const model = price.closest("tr").querySelector(`input[name="${list}_model"]`).value.trim();
        const entry = runtimes[model];
        const window = entry && form.elements[entry.window];
        const size = window ? Number(window.value) : 0;
        price.setCustomValidity(
          size && Number(price.value) > size
            ? `A price on ${model} cannot be more than the ${size} of the 5h window of ${entry.runtime}.`
            : ""
        );
      }
    }
    const coordinated = new Set();
    for (const input of form.querySelectorAll('input[name="own_role"]')) {
      const row = input.closest("tr");
      const role = input.value.trim();
      const model = row.querySelector('input[name="own_model"]').value.trim();
      const effort = row.querySelector('input[name="own_effort"]').value.trim();
      const key = JSON.stringify([role, model, effort]);
      input.setCustomValidity(
        role && coordinated.has(key)
          ? `Another row already has the role ${role} on ${model}${effort ? "/" + effort : ""}.`
          : ""
      );
      coordinated.add(key);
    }
  }

  document.addEventListener("input", (e) => {
    const row = e.target.closest && e.target.closest("[data-wallet]");
    if (row) syncReserve(row);
  });

  document.addEventListener("input", (e) => syncRequired(e.target.form));
  // While a wallet's Emulate 5h window box is ticked, its weekly limit and its 5h
  // window must each be above 0.
  function validateEmulatedWindow(form) {
    if (!form) return;
    for (const row of form.querySelectorAll("[data-wallet]")) {
      const box = row.querySelector("[data-emulate]");
      if (!box) continue;
      const size = row.querySelector("[data-window]");
      const ready = Number(row.querySelector("[data-week]").value) > 0 && Number(size.value) > 0;
      size.setCustomValidity(
        box.checked && !ready
          ? "Emulate 5h window needs a codex weekly limit and 5h window above 0."
          : ""
      );
    }
  }

  document.addEventListener("input", (e) => validatePriceLists(e.target.form));
  document.addEventListener("input", (e) => validateEmulatedWindow(e.target.form));
  document.addEventListener("htmx:afterSwap", () => {
    syncAllRequired(document);
    for (const form of document.querySelectorAll("form[data-price-runtimes]")) {
      validatePriceLists(form);
      validateEmulatedWindow(form);
    }
  });

  // ---- audio and 3D previews of files -----------------------------------------
  const MODEL_VIEWER_SRC = "/static/vendor/model-viewer-umd.min.js";
  let modelViewerLoading = null;

  // Resolves once <model-viewer> is defined; the script is fetched on the first call.
  function loadModelViewer() {
    if (customElements.get("model-viewer")) return Promise.resolve();
    if (!modelViewerLoading) {
      modelViewerLoading = new Promise((resolve) => {
        const script = document.createElement("script");
        script.src = MODEL_VIEWER_SRC;
        script.addEventListener("load", resolve);
        document.head.append(script);
      });
    }
    return modelViewerLoading;
  }

  // Shows `player` under `name` in the page's one file dialog, replacing what it held.
  function openFileDialog(name, player) {
    const dialog = document.getElementById("file-dialog");
    dialog.querySelector("[data-file-title]").textContent = name;
    dialog.querySelector("[data-file-body]").replaceChildren(player);
    if (!dialog.open) dialog.showModal();
  }

  // "View 3D" and "Listen" buttons open their file in the dialog.
  document.addEventListener("click", (e) => {
    const button = e.target.closest && e.target.closest("button.view-3d, button.listen");
    if (!button) return;
    const src = button.dataset.src;
    const name = button.dataset.name;
    if (button.classList.contains("listen")) {
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.autoplay = true;
      audio.src = src;
      openFileDialog(name, audio);
      return;
    }
    loadModelViewer().then(() => {
      const viewer = document.createElement("model-viewer");
      viewer.setAttribute("src", src);
      viewer.setAttribute("camera-controls", "");
      viewer.setAttribute("auto-rotate", "");
      viewer.setAttribute("shadow-intensity", "0.6");
      openFileDialog(name, viewer);
    });
  });

  // A click on the backdrop closes the file dialog.
  document.addEventListener("click", (e) => {
    const dialog = e.target;
    if (dialog.id !== "file-dialog") return;
    const box = dialog.getBoundingClientRect();
    const inside = e.clientX >= box.left && e.clientX <= box.right &&
      e.clientY >= box.top && e.clientY <= box.bottom;
    if (!inside) dialog.close();
  });

  // Closing the file dialog, by any means, empties it.
  document.getElementById("file-dialog").addEventListener("close", (e) => {
    e.target.querySelector("[data-file-body]").replaceChildren();
  });

  // Registered after every other afterSwap handler, so its time includes theirs.
  document.addEventListener("htmx:afterSwap", (e) => {
    const target = e.target;
    recentSwaps.push({
      at: Date.now(),
      target: (target && (target.id || target.className)) || "?",
      ms: performance.now() - swapStart,
    });
    if (recentSwaps.length > 50) recentSwaps.shift();
  });

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
