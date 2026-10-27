// Incremental transcript rows shared by the desktop and phone.
(function () {
  const states = new WeakMap();

  function renderRows(target, state) {
    const groups = [];
    for (const row of state.rows) {
      const last = groups.at(-1);
      if (row.kind === "text" && last?.kind === "text") {
        last.text += row.text;
        last.rows.push(row);
      } else {
        groups.push({kind: row.kind, text: (row.kind === "text" ? "" :
          (row.phase === "started" ? "running" : row.kind) + ": ") + row.text, rows: [row]});
      }
    }
    const nodes = new Map(), desired = [];
    if (state.noteNode) desired.push(state.noteNode);
    for (const group of groups) {
      let node = state.nodes.get(group.rows[0]);
      if (!node) {
        node = document.createElement("div");
        node.className = "tail-row " + group.kind;
        node.append(document.createTextNode(group.text));
      } else if (node.firstChild.data !== group.text) node.firstChild.data = group.text;
      for (const row of group.rows) nodes.set(row, node);
      desired.push(node);
    }
    const keep = new Set(desired);
    for (const child of [...target.children]) if (!keep.has(child)) child.remove();
    for (let index = 0; index < desired.length; index++) {
      if (target.children[index] !== desired[index]) target.insertBefore(desired[index], target.children[index] || null);
    }
    state.nodes = nodes;
  }

  window.officeRenderTranscript = function (target, data, noteClass) {
    let state = states.get(target);
    if (!state) { state = {rows: [], nodes: new Map(), noteNode: null}; states.set(target, state); }
    const stick = target.scrollHeight - target.scrollTop - target.clientHeight < 24;
    const top = target.scrollTop, height = target.scrollHeight;
    const minHeight = target.style.minHeight;
    target.style.minHeight = target.clientHeight + "px";
    let dropped = 0;
    if ("rows" in data) {
      state.rows = data.rows;
      state.nodes.clear();
      target.replaceChildren();
      state.noteNode = null;
    } else if (data.drop) {
      state.rows = state.rows.slice(data.drop);
      renderRows(target, state);
      dropped = Math.max(0, height - target.scrollHeight);
    }
    if (data.append) state.rows.push(...data.append);
    renderRows(target, state);
    if ("note" in data) {
      if (data.note) {
        if (!state.noteNode) { state.noteNode = document.createElement("div"); state.noteNode.className = noteClass; target.prepend(state.noteNode); }
        state.noteNode.textContent = data.note;
      } else { state.noteNode?.remove(); state.noteNode = null; }
    }
    target.style.minHeight = minHeight;
    target.scrollTop = stick ? target.scrollHeight : Math.max(0, top - dropped);
  };
})();
