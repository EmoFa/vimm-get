/* VimmGet frontend. Vanilla JS; state arrives via /api/state and stays fresh
   over the WebSocket. */

"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  queue: [],
  expanded: new Set(),   // queued rows opened for their disc picker
  history: [],
  jobs: {},      // id -> job
  settings: {},
  run: "idle",
  log: [],
  tag_vocabulary: [],
  lastHidden: [],
  prompt: null,   // the question the run is waiting on, if any
};

/* ------------------------------------------------------------------ api */

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

/* ------------------------------------------------------------- websocket */

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onclose = () => setTimeout(connectWS, 1500);
  ws.onmessage = (message) => {
    const event = JSON.parse(message.data);
    switch (event.type) {
      case "log":
        state.log.push(event.text);
        state.log = state.log.slice(-500);
        renderLog();
        break;
      case "item": {
        const i = state.queue.findIndex(q => q.vault_id === event.item.vault_id);
        if (i >= 0) state.queue[i] = event.item;
        renderActive();
        break;
      }
      case "queue":
        state.queue = event.queue;
        renderActive();
        break;
      case "prompt":
        state.prompt = event.prompt;
        renderPrompt();
        break;
      case "run":
        state.run = event.status;
        renderRun();
        break;
      case "job":
        state.jobs[event.job.id] = event.job;
        renderActive();
        break;
      case "history_item": {
        const i = state.history.findIndex(h => h.key === event.item.key);
        if (i >= 0) state.history[i] = event.item;
        else state.history.unshift(event.item);
        renderHistory();
        break;
      }
      case "history":
        state.history = event.history;
        renderHistory();
        break;
      case "settings":
        state.settings = event.settings;
        break;
      case "site":
        state.site = event.site;
        renderSite();
        break;
    }
  };
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
}

/* -------------------------------------------------------------- helpers */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bytes(n) {
  if (!n) return "";
  return n >= 1e9 ? (n / 1e9).toFixed(2) + " GB"
       : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB"
       : (n / 1e3).toFixed(0) + " KB";
}

function duration(seconds) {
  seconds = Math.max(Math.round(seconds), 0);
  if (seconds >= 3600) {
    return `${Math.floor(seconds / 3600)}h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}m`;
  }
  if (seconds >= 60) {
    return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  }
  return `${seconds}s`;
}

const CHIP = {
  queued: ["queued", "QUEUED"],
  working: ["downloading", "RESOLVING"],
  downloading: ["downloading", "DOWNLOADING"],
  paused: ["paused", "PAUSED"],
  done: ["done", "DONE"],
  skipped: ["done", "SKIPPED"],
  failed: ["failed", "FAILED"],
  listed: ["queued", "LISTED"],
};

/* --------------------------------------------------------------- active */

// Items being worked on get a full card; everything merely waiting gets one
// compact line, so a queue of fifty stays readable.
const BUSY = ["downloading", "working", "waiting", "paused"];

function discPicker(item) {
  const discs = item.discs || [];
  if (discs.length < 2) return null;
  const row = el("div", "discs");
  row.append(el("span", "disc-label", `${discs.length} DISCS`));
  for (const disc of discs) {
    const chip = el("label", "disc-chip" + (disc.selected ? " on" : ""));
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = disc.selected;
    box.disabled = ["downloading", "working", "waiting"].includes(item.status);
    box.onchange = () => {
      disc.selected = box.checked;
      chip.classList.toggle("on", box.checked);
      const wanted = discs.filter(d => d.selected).map(d => d.disc);
      api("POST", `/api/queue/${item.vault_id}/discs`, { discs: wanted })
        .catch(err => {
          // The run can reach this game between the click and the request,
          // and then the server refuses. Put the tick back where it was
          // rather than showing a choice that was not taken.
          disc.selected = !box.checked;
          box.checked = disc.selected;
          chip.classList.toggle("on", box.checked);
          alertErr(err);
        });
    };
    chip.append(box, el("span", null, `Disc ${disc.disc}`));
    if (disc.size_text) chip.append(el("span", "disc-label", disc.size_text));
    row.append(chip);
  }
  return row;
}

function queueControls(item) {
  const buttons = el("span", "card-buttons");
  const up = el("button", "icon-btn", "▲");
  const down = el("button", "icon-btn", "▼");
  up.onclick = (e) => { e.stopPropagation(); move(item.vault_id, -1); };
  down.onclick = (e) => { e.stopPropagation(); move(item.vault_id, +1); };
  const remove = el("button", "icon-btn x", "✕");
  remove.title = "Remove from queue";
  remove.onclick = (e) => {
    e.stopPropagation();
    api("DELETE", `/api/queue/${item.vault_id}`)
      .then(r => { state.queue = r.queue; renderActive(); });
  };
  buttons.append(up, down, remove);
  return buttons;
}

function queuedRow(item) {
  const open = state.expanded.has(item.vault_id);
  const row = el("div", "queue-row" + (open ? " open" : ""));

  const head = el("div", "queue-head");
  head.append(el("span", "queue-caret", open ? "▾" : "▸"));
  head.append(el("span", "queue-title", item.title));
  if (item.system) head.append(el("span", "sysbadge", item.system));
  if (item.size_text) head.append(el("span", "queue-size", item.size_text));
  if ((item.discs || []).length > 1)
    head.append(el("span", "disc-label", `${item.discs.length} discs`));
  if (item.status === "failed")
    head.append(el("span", "chip failed", "FAILED"));
  head.append(el("span", "grow"));
  if (item.message) head.append(el("span", "queue-note", item.message));
  head.append(queueControls(item));

  // Opening a row is also when its name gets looked up - nothing is fetched
  // just for sitting in the queue.
  head.onclick = () => {
    if (state.expanded.has(item.vault_id)) state.expanded.delete(item.vault_id);
    else {
      state.expanded.add(item.vault_id);
      if (!item.resolved) {
        item.message = "looking up...";
        api("POST", `/api/queue/${item.vault_id}/resolve`)
          .then(r => {
            const i = state.queue.findIndex(q => q.vault_id === item.vault_id);
            if (i >= 0 && r.item) state.queue[i] = r.item;
            renderActive();
          })
          .catch(alertErr);
      }
    }
    renderActive();
  };
  row.append(head);

  if (open) {
    const body = el("div", "queue-body");
    body.append(el("span", "meta", `vault/${item.vault_id}`));
    const picker = discPicker(item);
    if (picker) body.append(picker);
    row.append(body);
  }
  return row;
}

function renderActive() {
  const list = $("active-list");
  const queuedList = $("queued-list");
  list.replaceChildren();
  queuedList.replaceChildren();

  const busy = state.queue.filter(q => BUSY.includes(q.status));
  const waiting = state.queue.filter(q => !BUSY.includes(q.status));

  for (const item of waiting) queuedList.append(queuedRow(item));
  $("queued-count").textContent = waiting.length;
  $("queued-section").classList.toggle("hidden", waiting.length === 0);

  for (const item of busy) {
    const card = el("div", "card" + (item.status === "downloading" ? " working" : ""));
    const top = el("div", "card-top");

    if (item.status === "downloading" && item.progress > 0)
      top.append(el("span", "pct", (item.progress * 100).toFixed(1) + "%"));
    top.append(el("span", "card-title", item.title));

    let [chipClass, chipText] = CHIP[item.status] || ["queued", item.status.toUpperCase()];
    if (item.status === "waiting") {
      const left = Math.max((item.waiting_until || 0) - Date.now() / 1000, 0);
      chipClass = "waiting";
      chipText = item.waiting_kind === "disc"
        ? `NEXT DISC IN ${duration(left)}` : `PAUSED ${duration(left)}`;
    }
    top.append(el("span", `chip ${chipClass}`, chipText));

    top.append(queueControls(item));
    card.append(top);

    const sub = el("div", "card-sub");
    if (item.system) sub.append(el("span", "sysbadge", item.system));
    if (item.message) sub.append(el("span", "meta", item.message));
    if (item.status === "downloading" && item.speed > 0) {
      sub.append(el("span", "eta",
        `${bytes(item.speed)}/s · ETA ${duration(item.eta)}`));
    }
    card.append(sub);

    const picker = discPicker(item);
    if (picker) card.append(picker);

    if (["downloading", "working", "paused", "waiting"].includes(item.status)) {
      const bar = el("div", "bar" + (item.status === "waiting" ? " waiting" : ""));
      const fill = el("div");
      fill.style.width = (item.progress * 100).toFixed(1) + "%";
      bar.append(fill);
      card.append(bar);
    }
    list.append(card);
  }

  // Pipeline jobs render as active cards too (extract / convert).
  for (const job of Object.values(state.jobs)) {
    if (job.status === "done" || job.status === "failed") continue;
    const card = el("div", "card");
    const top = el("div", "card-top");
    top.append(el("span", "pct", (job.progress * 100).toFixed(0) + "%"));
    top.append(el("span", "card-title", job.label));
    const kind = job.kind === "chd" ? ["convert", "CONVERTING"]
               : job.kind === "m3u" ? ["convert", "PLAYLIST"]
               : ["extract", "EXTRACTING"];
    top.append(el("span", `chip ${kind[0]}`, kind[1]));
    card.append(top);
    if (job.message) card.append(el("div", "meta", job.message));
    const bar = el("div", `bar ${kind[0] === "convert" ? "convert" : ""}`);
    const fill = el("div");
    fill.style.width = (job.progress * 100).toFixed(1) + "%";
    bar.append(fill);
    card.append(bar);
    list.append(card);
  }

  const active = busy.length
    + Object.values(state.jobs).filter(j => j.status !== "done" && j.status !== "failed").length;
  $("active-count").textContent = active;
  renderRun();
}

function move(vaultId, delta) {
  const order = state.queue.map(q => q.vault_id);
  const i = order.indexOf(vaultId);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  api("POST", "/api/queue/reorder", { order })
    .then(r => { state.queue = r.queue; renderActive(); });
}

// Keeps the "NEXT DISC IN 6s" countdown ticking between WebSocket events.
setInterval(() => {
  if (state.queue.some(q => q.status === "waiting")) renderActive();
}, 1000);

/* -------------------------------------------------------------- history */

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  $("history-count").textContent = state.history.length;

  for (const entry of state.history) {
    const files = entry.files || [];
    const totalBytes = files.reduce((sum, f) => sum + (f.bytes || 0), 0);
    const card = el("div", "card");

    const top = el("div", "card-top");
    top.append(el("span", "card-title", entry.title));
    top.append(el("span", "grow"));
    if (files.length > 1)
      top.append(el("span", "disc-label", `${files.length} DISCS`));
    if (entry.system_folder) top.append(el("span", "sysbadge", entry.system_folder));
    top.append(el("span", "size", bytes(totalBytes)));

    const stages = entry.stages || {};
    const buttons = el("span", "card-buttons");
    if (!stages.extracted) {
      const b = el("button", "hbtn", "EXTRACT");
      b.onclick = () => api("POST", `/api/items/${entry.key}/extract`).catch(alertErr);
      buttons.append(b);
    } else {
      // The server's flags already account for the stage being done, so
      // there is one place that decides whether an action is offered.
      if (entry.can_chd) {
        const b = el("button", "hbtn", "COMPRESS");
        b.onclick = () => api("POST", `/api/items/${entry.key}/chd`).catch(alertErr);
        buttons.append(b);
      }
      if (entry.can_m3u) {
        const b = el("button", "hbtn", "M3U");
        b.onclick = () => api("POST", `/api/items/${entry.key}/m3u`).catch(alertErr);
        buttons.append(b);
      }
    }
    const remove = el("button", "icon-btn x", "✕");
    remove.title = "Remove from history (files stay on disk)";
    remove.onclick = () => api("DELETE", `/api/history/${entry.key}`)
      .then(r => { state.history = r.history; renderHistory(); });
    buttons.append(remove);
    top.append(buttons);
    card.append(top);

    // Discs listed under the one parent card.
    const fileBox = el("div", "files");
    for (const file of files) {
      const row = el("div", "file-row");
      if (files.length > 1) row.append(el("span", "disc-no", `DISC ${file.disc}`));
      row.append(el("span", "fname", file.filename));
      row.append(el("span", "fsize", bytes(file.bytes)));
      fileBox.append(row);
    }
    card.append(fileBox);

    const stageBox = el("div", "stages");
    stageBox.append(el("span", "stage-tick", "✓ DOWNLOADED"));
    stageBox.append(el("span", "stage-tick" + (stages.extracted ? "" : " todo"),
                       (stages.extracted ? "✓" : "·") + " EXTRACTED"));
    if (entry.can_chd || stages.chd) {
      stageBox.append(el("span", "stage-tick" + (stages.chd ? "" : " todo"),
                         (stages.chd ? "✓" : "·") + " CHD"));
    }
    if (entry.can_m3u || stages.m3u) {
      stageBox.append(el("span", "stage-tick" + (stages.m3u ? "" : " todo"),
                         (stages.m3u ? "✓" : "·") + " M3U"));
    }
    card.append(stageBox);
    list.append(card);
  }
}

/* ------------------------------------------------------------ run + log */

function renderRun() {
  const running = ["running", "pausing", "stopping"].includes(state.run);
  const pending = state.queue.some(q => !["done", "skipped"].includes(q.status));
  $("start-btn").disabled = running || !pending;
  $("pause-btn").disabled = !running;
  // Stop also clears partial downloads left by an earlier session, so it
  // stays available whenever there is a queue to act on.
  $("stop-btn").disabled = !running && state.queue.length === 0;
  $("run-status").textContent = state.run === "idle" ? "" : state.run;
}

/* ------------------------------------------------------------ site status */

const SITE_LABELS = {
  up: "Vimm online",
  maintenance: "Maintenance",
  down: "Vimm unreachable",
  checking: "Checking...",
  unknown: "Vimm status",
};

function renderSite() {
  const site = state.site || { state: "unknown", detail: "", checked_at: 0 };
  const button = $("site-status");
  button.className = `sitestatus ${site.state}`;
  $("site-label").textContent = SITE_LABELS[site.state] || SITE_LABELS.unknown;

  let tip = site.detail || "";
  if (site.checked_at && site.state !== "checking") {
    const ago = Math.max(Math.round(Date.now() / 1000 - site.checked_at), 0);
    tip += ago < 60 ? `  (checked just now)` : `  (checked ${duration(ago)} ago)`;
  }
  button.title = `${tip}\nClick to check again`;
}

function renderLog() {
  const log = $("log");
  log.textContent = state.log.join("\n");
  log.scrollTop = log.scrollHeight;
}

function alertErr(err) { state.log.push("! " + err.message); renderLog(); }

/* ------------------------------------------------------- omnibox + search */

function looksLikeIds(text) {
  return text.split("\n").some(line => {
    line = line.split("#")[0].trim();
    return /^\d+$/.test(line) || /\/vault\/\d+/.test(line);
  });
}

async function onAdd() {
  const text = $("omni").value.trim();
  if (!text) return;
  if (looksLikeIds(text)) {
    const r = await api("POST", "/api/queue", { text });
    state.queue = r.queue;
    renderActive();
    $("omni").value = "";
    sizeOmni();
    $("search-panel").classList.add("hidden");
  } else {
    await runSearch(text);
  }
}

function resultRow(hit, dimmed) {
  const row = el("div", "result-row" + (dimmed ? " is-hidden" : ""));
  row.append(el("span", "result-meta", String(hit.vault_id)));
  row.append(el("span", "result-title", hit.title));
  row.append(el("span", "sysbadge", hit.system));
  row.append(el("span", "result-meta",
                (hit.regions || []).join("/") + (hit.version ? "  v" + hit.version : "")));
  for (const tag of hit.tags || []) {
    row.append(el("span", "tagchip" + (hit.downloadable ? "" : " blocked"), tag));
  }
  row.append(el("span", "grow"));
  if (hit.downloadable) {
    const add = el("button", "hbtn", "+ ADD");
    add.onclick = () =>
      api("POST", "/api/queue", { hits: [hit] })
        .then(res => { state.queue = res.queue; renderActive(); add.textContent = "ADDED"; });
    row.append(add);
  } else {
    // Vimm says there is no file behind this entry, so there is nothing to queue.
    row.append(el("span", "result-meta", "no download"));
  }
  return row;
}

async function runSearch(query) {
  try {
    const r = await api("GET", `/api/search?q=${encodeURIComponent(query)}`);
    state.tag_vocabulary = r.tag_vocabulary || state.tag_vocabulary;
    state.lastHidden = r.hidden || [];

    const box = $("search-results");
    box.replaceChildren();
    $("search-count").textContent = `${r.hits.length} result(s)`;
    for (const hit of r.hits) box.append(resultRow(hit, false));

    const hiddenBox = $("hidden-results");
    hiddenBox.replaceChildren();
    for (const hit of state.lastHidden) hiddenBox.append(resultRow(hit, true));
    hiddenBox.classList.add("hidden");
    $("hidden-toggle").textContent = "SHOW";
    $("hidden-count").textContent =
      `${state.lastHidden.length} hidden by your filters`;
    $("hidden-bar").classList.toggle("hidden", state.lastHidden.length === 0);

    $("search-panel").classList.remove("hidden");
  } catch (err) { alertErr(err); }
}

/* -------------------------------------------------------------- settings */

const SETTING_FIELDS = {
  out: ["set-out", "text"],
  organize: ["set-organize", "check"],
  prefer: ["set-prefer", "text"],
  version_policy: ["set-policy", "text"],
  disc_policy: ["set-discpolicy", "text"],
  delay: ["set-delay", "number"],
  sweeps: ["set-sweeps", "number"],
  cancel_busy: ["set-cancelbusy", "check"],
  cookies: ["set-cookies", "text"],
  auto_extract: ["set-autoextract", "check"],
  auto_compress: ["set-autocompress", "check"],
  auto_m3u: ["set-autom3u", "check"],
  delete_chd_sources: ["set-delchd", "check"],
  wait_for_processing: ["set-waitprocess", "check"],
};

// Per-system download format, keyed by the folder name the server uses.
const FORMAT_FIELDS = { gc: "set-fmt-gc", wii: "set-fmt-wii",
                        xbox: "set-fmt-xbox", ps3: "set-fmt-ps3" };

function loadSettingsForm() {
  for (const [key, [id, kind]] of Object.entries(SETTING_FIELDS)) {
    const node = $(id);
    if (kind === "check") node.checked = !!state.settings[key];
    else node.value = state.settings[key] ?? "";
  }
  $("set-m3usystems").value = (state.settings.m3u_systems || []).join(", ");
  $("set-chdsystems").value = (state.settings.chd_systems || []).join(", ");
  const formats = state.settings.formats || {};
  for (const [system, id] of Object.entries(FORMAT_FIELDS))
    if (formats[system]) $(id).value = formats[system];

  // Tag filter checkboxes, built from what the site actually uses.
  const box = $("tag-filters");
  box.replaceChildren();
  const hidden = (state.settings.hidden_tags || []).map(t => t.toLowerCase());
  for (const tag of state.tag_vocabulary) {
    const label = el("label", "checkline");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.tag = tag;
    input.checked = hidden.some(h => tag.toLowerCase().startsWith(h)
                                     || h.startsWith(tag.toLowerCase()));
    label.append(input, el("span", null, `Hide "${tag}" entries`));
    box.append(label);
  }
}

async function saveSettings() {
  const body = {};
  for (const [key, [id, kind]] of Object.entries(SETTING_FIELDS)) {
    const node = $(id);
    body[key] = kind === "check" ? node.checked
              : kind === "number" ? Number(node.value)
              : node.value;
  }
  const csv = (id) => $(id).value.split(",").map(s => s.trim()).filter(Boolean);
  body.m3u_systems = csv("set-m3usystems");
  body.chd_systems = csv("set-chdsystems");
  body.hidden_tags = [...$("tag-filters").querySelectorAll("input:checked")]
    .map(input => input.dataset.tag);
  body.formats = Object.fromEntries(
    Object.entries(FORMAT_FIELDS).map(([system, id]) => [system, $(id).value]));
  state.settings = await api("PUT", "/api/settings", body);
  $("settings-panel").classList.add("hidden");
}

/* ----------------------------------------------------------------- wiring */

$("add-btn").onclick = () => onAdd().catch(alertErr);
$("omni").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); onAdd().catch(alertErr); }
});
// Grow with a pasted list rather than hiding it behind a one-line box.
function sizeOmni() {
  const box = $("omni");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 190) + "px";
}
$("omni").addEventListener("input", sizeOmni);
$("omni").addEventListener("paste", () => setTimeout(sizeOmni, 0));
$("search-close").onclick = () => $("search-panel").classList.add("hidden");
$("hidden-toggle").onclick = () => {
  const box = $("hidden-results");
  const nowHidden = box.classList.toggle("hidden");
  $("hidden-toggle").textContent = nowHidden ? "SHOW" : "HIDE";
};

/* ------------------------------------------------------------- ask me */
/* Only ever seen by someone who set Revision or Discs to "ask me". The run
   is stopped at that game and stays stopped until this is answered, so the
   quick answer - everything ticked, one click - is the default. */

function renderPrompt() {
  const p = state.prompt;
  $("prompt-modal").classList.toggle("hidden", !p);
  if (!p) return;

  $("prompt-title").textContent = p.title || "";
  const body = $("prompt-body");
  body.replaceChildren();

  if (p.kind === "discs") {
    $("prompt-head").textContent = "CHOOSE DISCS";
    $("prompt-hint").textContent =
      "This game comes on more than one disc. Untick any you do not want.";
    const row = el("div", "discs");
    for (const disc of p.discs) {
      const chip = el("label", "disc-chip on");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.dataset.disc = disc.disc;
      box.onchange = () => chip.classList.toggle("on", box.checked);
      chip.append(box, el("span", null, `Disc ${disc.disc}`));
      if (disc.size_text) chip.append(el("span", "disc-label", disc.size_text));
      row.append(chip);
    }
    body.append(row);
  } else {
    $("prompt-head").textContent = "CHOOSE A REVISION";
    $("prompt-hint").textContent =
      `Disc ${p.disc} has more than one revision. Pick the one to download.`;
    const row = el("div", "discs");
    for (const [index, version] of (p.versions || []).entries()) {
      const chip = el("label", "disc-chip" + (index === 0 ? " on" : ""));
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "prompt-version";
      radio.checked = index === 0;
      radio.dataset.mediaId = version.media_id;
      radio.onchange = () => {
        for (const other of row.querySelectorAll(".disc-chip"))
          other.classList.remove("on");
        chip.classList.add("on");
      };
      chip.append(radio, el("span", null, `v${version.version}`));
      if (version.size_text)
        chip.append(el("span", "disc-label", version.size_text));
      row.append(chip);
    }
    body.append(row);
  }
}

async function answerPrompt(answer) {
  const p = state.prompt;
  if (!p) return;
  state.prompt = null;
  renderPrompt();
  try { await api("POST", `/api/prompt/${p.id}`, { answer }); }
  catch (err) { alertErr(err); }
}

$("prompt-skip").onclick = () => answerPrompt("skip");
$("prompt-go").onclick = () => {
  const boxes = [...$("prompt-body").querySelectorAll("input:checked")];
  answerPrompt(boxes.map(b => Number(b.dataset.disc ?? b.dataset.mediaId)));
};

$("start-btn").onclick = () => api("POST", "/api/run/start").catch(alertErr);
$("pause-btn").onclick = () => api("POST", "/api/run/pause").catch(alertErr);
// Stop throws partial downloads away - the one destructive action here - so
// it always asks first, and says what it would cost.
$("stop-btn").onclick = async () => {
  try {
    const p = await api("GET", "/api/run/partials");
    const what = p.count
      ? `Discard ${p.count} partial download${p.count === 1 ? "" : "s"} (${p.human})?`
      : "Stop the run?";
    const detail = p.count
      ? "\n\nThose bytes are lost and the affected games start again from the "
        + "beginning next time.\nUse Pause instead to keep them and resume exactly."
      : "";
    if (!confirm(what + detail)) return;
    await api("POST", "/api/run/stop");
  } catch (err) { alertErr(err); }
};
$("clear-btn").onclick = () => api("POST", "/api/queue/clear")
  .then(r => { state.queue = r.queue; renderActive(); });
// It sits inside the section header, so without this it would collapse the
// section on the way past.
$("convert-all").onclick = (event) => {
  event.stopPropagation();
  api("POST", "/api/convert-all")
    .then(r => { state.log.push(`convert all: ${r.submitted} job(s) submitted`); renderLog(); })
    .catch(alertErr);
};
$("history-toggle").onclick = () => {
  const open = !$("history-list").classList.toggle("hidden");
  $("history-caret").textContent = open ? "▾" : "▸";
};

$("site-status").onclick = () => {
  state.site = { state: "checking", detail: "contacting vimm.net...", checked_at: 0 };
  renderSite();
  api("POST", "/api/site/check").catch(alertErr);
};
$("settings-btn").onclick = () => { loadSettingsForm(); $("settings-panel").classList.remove("hidden"); };
$("settings-close").onclick = () => $("settings-panel").classList.add("hidden");
$("settings-save").onclick = () => saveSettings().catch(alertErr);
$("log-toggle").onclick = () => $("log").classList.toggle("hidden");

/* ------------------------------------------------------------------ boot */

(async function boot() {
  const snapshot = await api("GET", "/api/state");
  Object.assign(state, snapshot);
  state.jobs = Object.fromEntries((snapshot.jobs || []).map(j => [j.id, j]));
  renderActive();
  renderHistory();
  renderSite();
  renderLog();
  renderPrompt();
  connectWS();
})();
