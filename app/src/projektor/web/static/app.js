/* Projektor web UI - plain JS, no build step, works offline. */
"use strict";

const S = {
  auth: null, info: null, show: null, state: null, videos: null,
  selCue: null, editVideo: null, editCue: null, videoDraft: null,
  ws: null, wsBackoff: 500, liveKey: "", tab: "live", upload: null, bkPoll: null,
};

const OSC_REF = [
  ["/proj/go", "-", "If paused at a pause point: resume. Else run the standby cue and advance standby."],
  ["/proj/cue", "ref", "Run cue ref (idempotent: already playing = no restart). Standby := next."],
  ["/proj/standby", "ref", "Set standby cue, no playback change."],
  ["/proj/play", "video_id [fade_in_s]", "Play video directly (no cue), defaults from video entry."],
  ["/proj/pause", "-", "Freeze current frame."],
  ["/proj/resume", "-", "Resume from pause (manual or pause point)."],
  ["/proj/fadeout", "[seconds]", "Fade to black, then stop. Default = cue/video fade_out."],
  ["/proj/stop", "-", "Cut to black immediately."],
  ["/proj/seek", "seconds", "Seek in current video."],
  ["/proj/status", "0|1", "Hide/show status screen (only when nothing plays)."],
  ["/proj/reload", "-", "Reload show.json."],
  ["/proj/ping", "-", "Reply /proj/pong <device_name>."],
  ["/proj/state", "-", "Reply /proj/state <json-string>."],
  ["/proj/beamer/power", "on|off|1|0", "Beamer on (HDMI-CEC image view on + active source) or standby."],
  ["/proj/beamer/status", "-", "Reply /proj/beamer/status on|standby|unknown|unavailable."],
  ["/proj/beamer/key", "name", "CEC key: up down left right select back home menu volume-up volume-down mute."],
];

// ------------------------------------------------------------------ helpers

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function fmtTime(s) {
  if (s === null || s === undefined || isNaN(s)) return "-";
  const neg = s < 0; s = Math.abs(s);
  const m = Math.floor(s / 60), sec = s - m * 60;
  const h_ = Math.floor(m / 60);
  const mm = h_ ? String(m % 60).padStart(2, "0") : String(m);
  return (neg ? "-" : "") + (h_ ? h_ + ":" : "") + mm + ":" + sec.toFixed(1).padStart(4, "0");
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + " " + u[i];
}

function fmtDur(s) {
  if (s === null || s === undefined) return "-";
  s = Math.round(s);
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
  return (d ? d + "d " : "") + (d || hh ? hh + "h " : "") + mm + "m";
}

function fmtAge(ts) {
  if (!ts) return "-";
  const a = Date.now() / 1000 - ts;
  if (a < 60) return Math.round(a) + " s ago";
  if (a < 3600) return Math.round(a / 60) + " min ago";
  return new Date(ts * 1000).toLocaleString();
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), isErr ? 5000 : 2200);
}

function errText(d) {
  if (!d) return "";
  if (typeof d === "string") return d;
  const det = d.detail !== undefined ? d.detail : d;
  if (typeof det === "string") return det;
  if (Array.isArray(det)) return det.map((e) => (e.loc ? e.loc.join(".") + ": " : "") + (e.msg || JSON.stringify(e))).join("; ");
  if (det && det.msg) return det.msg;
  return JSON.stringify(det);
}

async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new Error("connection failed"); }
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const err = new Error(errText(data) || r.statusText);
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast(e.message, true);
    return undefined;
  }
}

function num(v) {
  if (v === "" || v === null || v === undefined) return null;
  const f = parseFloat(String(v).replace(",", "."));
  return isNaN(f) ? null : f;
}

function cueIndex(id) {
  return S.show ? S.show.cues.findIndex((c) => String(c.id) === String(id)) : -1;
}

function cueLabel(id) {
  if (id === null || id === undefined) return "-";
  const i = cueIndex(id);
  if (i < 0) return String(id);
  const c = S.show.cues[i];
  return `#${i + 1}  ${c.name || "(unnamed)"}` + (String(c.id) !== String(i + 1) ? `  [id ${c.id}]` : "");
}

function videoTitle(id) {
  const v = S.show && S.show.videos[id];
  return v ? (v.title || id) : id;
}

// ------------------------------------------------------------------ auth

function showLogin() {
  $("login").classList.remove("hidden");
  setTimeout(() => $("loginPw").focus(), 50);
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginErr").textContent = "";
  try {
    await api("POST", "/api/login", { password: $("loginPw").value });
    $("loginPw").value = "";
    $("login").classList.add("hidden");
    start();
  } catch (e) {
    $("loginErr").textContent = e.message;
  }
});

// ------------------------------------------------------------------ tabs

function setTab(name) {
  if (!$("tab-" + name)) name = "live";
  S.tab = name;
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("section.tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  if (name === "live") { loadShow(); loadBeamer(); }
  if (name === "videos") loadVideos();
  if (name === "cues") loadShow().then(renderCues);
  if (name === "system") { loadSystem(); loadWifi(); loadBackup(); }
}
document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

// ------------------------------------------------------------------ live state

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  S.ws = ws;
  ws.onopen = () => { $("wsDot").classList.add("ok"); S.wsBackoff = 500; };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "state") onState(msg.state);
  };
  ws.onclose = (ev) => {
    $("wsDot").classList.remove("ok");
    if (S.ws !== ws) return;
    if (ev.code === 4401) { showLogin(); return; }
    setTimeout(connectWS, S.wsBackoff);
    S.wsBackoff = Math.min(S.wsBackoff * 2, 5000);
  };
}
setInterval(() => { if (S.ws && S.ws.readyState === 1) S.ws.send("ping"); }, 20000);

function effectivePausePoints(st) {
  if (!S.show || !st.video) return [];
  const i = st.cue !== null && st.cue !== undefined ? cueIndex(st.cue) : -1;
  if (i >= 0 && S.show.cues[i].pause_points) return S.show.cues[i].pause_points;
  const v = S.show.videos[st.video];
  return v ? v.pause_points || [] : [];
}

function onState(st) {
  S.state = st;
  const state = st.state || "idle";
  const label = state === "paused" && st.pause_reason === "pause_point" ? "paused @pp" : state;
  for (const id of ["hdrState", "nowState"]) {
    $(id).textContent = label;
    $(id).className = "badge " + state;
  }
  if (st.cue !== null && st.cue !== undefined) {
    $("nowCue").textContent = cueLabel(st.cue);
  } else {
    $("nowCue").textContent = st.video ? "(direct play)" : "-";
  }
  $("nowVideo").textContent = st.video ? `${videoTitle(st.video)}  [${st.video}]` +
    (st.pause_reason ? `  - paused (${st.pause_reason.replace("_", " ")})` : "") : (st.status_screen ? "status screen" : "black");
  const pos = st.position || 0, dur = st.duration;
  $("nowPos").textContent = fmtTime(pos);
  $("nowDur").textContent = fmtTime(dur);
  $("nowRemain").textContent = dur ? "-" + fmtTime(Math.max(0, dur - pos)) : "";
  $("nowBar").style.width = dur ? Math.min(100, (pos / dur) * 100) + "%" : "0%";
  const ppKey = JSON.stringify([st.video, st.cue, dur, effectivePausePoints(st)]);
  const wrap = $("nowBarWrap");
  if (wrap.dataset.pp !== ppKey) {
    wrap.dataset.pp = ppKey;
    wrap.querySelectorAll(".pp").forEach((e) => e.remove());
    if (dur) for (const p of effectivePausePoints(st)) {
      if (p <= dur) wrap.append(h("div", { class: "pp", style: `left:${(p / dur) * 100}%` }));
    }
  }
  $("nowStandby").innerHTML = "";
  $("nowStandby").append("Standby: ", h("b", {}, st.standby !== null && st.standby !== undefined ? cueLabel(st.standby) : "-"));
  $("nowError").textContent = st.error || "";
  $("nowError").classList.toggle("hidden", !st.error);
  $("btnPause").textContent = state === "paused" ? "RESUME" : "PAUSE";
  $("statusToggle").checked = !!st.status_screen;

  const lc = st.last_command;
  const kv = $("lastCmd"); kv.innerHTML = "";
  if (lc) {
    kv.append(h("span", {}, "Address"), h("span", { class: "mono" }, lc.address + " " + (lc.args || []).map((a) => JSON.stringify(a)).join(" ")),
      h("span", {}, "Source"), h("span", {}, lc.source || "-"),
      h("span", {}, "Time"), h("span", {}, lc.time ? new Date(lc.time * 1000).toLocaleTimeString() : "-"));
  } else kv.append(h("span", {}, "-"), h("span", {}, ""));
  const key = `${st.cue}|${st.standby}|${state}`;
  if (key !== S.liveKey) { S.liveKey = key; renderLiveCues(); }
  if (S.tab === "system") renderTransports();
}

function renderLiveCues() {
  const ul = $("liveCues"); ul.innerHTML = "";
  if (!S.show || !S.show.cues.length) { ul.append(h("li", { class: "muted" }, "No cues yet - add some in the Cues tab.")); return; }
  const st = S.state || {};
  S.show.cues.forEach((c, i) => {
    const cur = st.cue !== null && String(st.cue) === String(c.id) && st.state !== "idle";
    const sb = st.standby !== null && String(st.standby) === String(c.id);
    const li = h("li", { class: [cur ? "current" : "", sb ? "standby" : "", S.selCue === c.id ? "sel" : ""].join(" ") },
      h("span", { class: "num" }, i + 1),
      h("span", { class: "nm" }, c.name || "(unnamed)", cur ? h("span", { class: "tag cur" }, "NOW") : null,
        sb ? h("span", { class: "tag sb" }, "STANDBY") : null,
        h("small", {}, `id ${c.id} - ${videoTitle(c.video)}`)),
      h("span", { class: "acts" },
        h("button", { class: "small primary", onclick: (e) => { e.stopPropagation(); transport("cue", { ref: c.id }); S.selCue = null; } }, "Run"),
        h("button", { class: "small", onclick: (e) => { e.stopPropagation(); transport("standby", { ref: c.id }); S.selCue = null; } }, "Standby")));
    li.addEventListener("click", () => { S.selCue = S.selCue === c.id ? null : c.id; renderLiveCues(); });
    ul.append(li);
  });
}

let lastPress = 0;
async function transport(action, body) {
  const now = Date.now();
  if (action === "go" && now - lastPress < 400) return; // double-tap guard
  lastPress = now;
  try {
    const r = await api("POST", "/api/transport/" + action, body);
    if (r && r.state) onState(r.state);
  } catch (e) { toast(e.message, true); }
}

$("btnGo").addEventListener("click", () => transport("go"));
$("btnPause").addEventListener("click", () => transport(S.state && S.state.state === "paused" ? "resume" : "pause"));
$("btnFade").addEventListener("click", () => transport("fadeout", {}));
$("btnStop").addEventListener("click", () => transport("stop"));
$("btnReload").addEventListener("click", async () => { await transport("reload"); await loadShow(); toast("show reloaded"); });
document.querySelectorAll("[data-seek]").forEach((b) => b.addEventListener("click", () => {
  if (!S.state || !S.state.video) return toast("nothing playing", true);
  transport("seek", { seconds: Math.max(0, (S.state.position || 0) + parseFloat(b.dataset.seek)) });
}));
$("statusToggle").addEventListener("change", (e) => transport("status", { on: e.target.checked }));

const cmdHistory = [];
$("cmdHints").append(...OSC_REF.map((r) => h("option", { value: r[0] })));
$("cmdForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const line = $("cmdLine").value.trim();
  if (!line) return;
  let out;
  try {
    const r = await api("POST", "/api/command", { line });
    const rep = r.replies.length ? r.replies.map((x) => `  <- ${x.address} ${x.args.map((a) => JSON.stringify(a)).join(" ")}`).join("\n") : "  (no reply)";
    out = `-> ${r.address} ${r.args.map((a) => JSON.stringify(a)).join(" ")}\n${rep}`;
  } catch (e) { out = `-> ${line}\n  !! ${e.message}`; }
  cmdHistory.unshift(out);
  cmdHistory.length = Math.min(cmdHistory.length, 8);
  $("cmdReplies").textContent = cmdHistory.join("\n");
});

// ------------------------------------------------------------------ show

async function loadShow() {
  try {
    S.show = await api("GET", "/api/show");
    S.liveKey = "";
    if (S.state) onState(S.state); else renderLiveCues();
  } catch (e) { if (e.status !== 401) toast("show: " + e.message, true); }
  return S.show;
}

// ------------------------------------------------------------------ videos

async function loadVideos() {
  $("videoList").innerHTML = '<p class="muted small">loading (probing files)...</p>';
  try {
    S.videos = await api("GET", "/api/videos");
    await loadShow();
    renderVideos();
  } catch (e) { $("videoList").textContent = e.message; }
}
$("vidRefresh").addEventListener("click", loadVideos);

function probeLine(p) {
  if (!p || p.error) return null;
  const parts = [];
  if (p.codec_name || p.codec) parts.push(p.codec_name || p.codec);
  if (p.width) parts.push(`${p.width}x${p.height}`);
  if (p.fps) parts.push(`${p.fps} fps`);
  if (p.duration) parts.push(fmtTime(p.duration));
  if (p.container) parts.push(p.container);
  return parts.join(" - ");
}

function renderVideos() {
  const d = S.videos; if (!d) return;
  if (d.disk) $("diskInfo").textContent = `Disk: ${fmtBytes(d.disk.free)} free of ${fmtBytes(d.disk.total)} - max upload ${d.max_upload_mb} MB`;
  const list = $("videoList"); list.innerHTML = "";
  if (!d.videos.length) list.append(h("p", { class: "muted" }, "No videos yet. Upload one above."));
  for (const v of d.videos) list.append(videoItem(v));
  const ol = $("orphanList"); ol.innerHTML = "";
  $("orphanCard").classList.toggle("hidden", !d.files.length);
  for (const f of d.files) {
    ol.append(h("div", { class: "item" },
      h("div", { class: "title" }, f.name, preparedTag(f.probe)),
      h("div", { class: "sub" }, fmtBytes(f.size), probeLine(f.probe) ? " - " + probeLine(f.probe) : ""),
      (f.warnings || []).map((w) => h("div", { class: "warn" }, w)),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "small primary", onclick: () => act(async () => { await api("POST", "/api/videos", { file: f.name }); await loadVideos(); }, "video added") }, "Add as video"),
        h("button", { class: "small danger", onclick: () => { if (confirm(`Delete file ${f.name}?`)) act(async () => { await api("DELETE", "/api/files/" + encodeURIComponent(f.name)); await loadVideos(); }, "deleted"); } }, "Delete file"))));
  }
}

function preparedTag(p) {
  if (!p || p.error || !p.prepared) return null;
  const ki = p.keyframe_interval ? `, keyframe ${Number(p.keyframe_interval).toFixed(2)} s` : "";
  return h("span", { class: "tag ok", title: "transcoded with projektor-prepare" + ki }, "prepared");
}

function videoItem(v) {
  const el = h("div", { class: "item" },
    h("div", { class: "row" },
      h("div", { class: "grow" },
        h("div", { class: "title" }, v.title || v.id, h("span", { class: "muted small" }, `  [${v.id}]`), preparedTag(v.probe)),
        h("div", { class: "sub" }, `${v.file} - ${fmtBytes(v.size)}`, probeLine(v.probe) ? h("br") : null, probeLine(v.probe) || ""),
        h("div", { class: "sub" },
          `in ${fmtTime(v.in_point)} - out ${v.out_point === null ? "end" : fmtTime(v.out_point)} - fade ${v.fade_in}/${v.fade_out} s - end: ${v.end}` +
          (v.pause_points && v.pause_points.length ? ` - pauses: ${v.pause_points.map(fmtTime).join(", ")}` : "") +
          (v.used_by.length ? ` - cues: ${v.used_by.join(", ")}` : " - not used by any cue")))),
    (v.warnings || []).map((w) => h("div", { class: "warn" }, w)),
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "small", onclick: () => { S.editVideo = S.editVideo === v.id ? null : v.id; S.videoDraft = null; renderVideos(); } }, S.editVideo === v.id ? "Close" : "Edit"),
      h("button", { class: "small", onclick: () => { if (confirm(`Play "${v.title || v.id}" on the projector now?`)) transport("play", { video: v.id }); } }, "Play on projector"),
      h("span", { class: "grow" }),
      h("button", { class: "small danger", onclick: () => deleteVideo(v) }, "Delete")));
  if (S.editVideo === v.id) el.append(videoEditor(v));
  return el;
}

async function deleteVideo(v) {
  if (!confirm(`Delete video "${v.id}" and its file ${v.file}?`)) return;
  try {
    await api("DELETE", `/api/videos/${encodeURIComponent(v.id)}`);
  } catch (e) {
    if (e.status !== 409) return toast(e.message, true);
    const cues = (e.data && e.data.detail && e.data.detail.cues) || [];
    if (!confirm(`${e.message}.\n\nDelete the video AND cue(s) ${cues.join(", ")}?`)) return;
    try { await api("DELETE", `/api/videos/${encodeURIComponent(v.id)}?force=true`); } catch (e2) { return toast(e2.message, true); }
  }
  toast("deleted");
  await loadVideos();
}

function videoEditor(v) {
  if (!S.videoDraft || S.videoDraft.id !== v.id) S.videoDraft = { id: v.id, pause_points: [...(v.pause_points || [])] };
  const dr = S.videoDraft;
  const inp = (key, val, attrs) => h("input", Object.assign({ "data-k": key, value: val === null || val === undefined ? "" : val, inputmode: "decimal" }, attrs || {}));
  const chips = h("div", { class: "chips" });
  const renderChips = () => {
    chips.innerHTML = "";
    if (!dr.pause_points.length) chips.append(h("span", { class: "muted small" }, "no pause points"));
    dr.pause_points.sort((a, b) => a - b).forEach((p, i) => chips.append(h("span", { class: "chip" }, fmtTime(p) + ` (${p})`,
      h("button", { title: "remove", onclick: () => { dr.pause_points.splice(i, 1); renderChips(); } }, "x"))));
  };
  renderChips();
  const ppInput = h("input", { placeholder: "seconds, e.g. 12.5", inputmode: "decimal" });
  const addPP = (p) => {
    if (p === null || p < 0) return toast("invalid time", true);
    p = Math.round(p * 1000) / 1000;
    if (!dr.pause_points.includes(p)) dr.pause_points.push(p);
    renderChips();
  };
  const ed = h("div", { class: "editor" },
    h("div", { class: "grid2" },
      h("label", { class: "f" }, "Title", inp("title", v.title, { inputmode: null })),
      h("label", { class: "f" }, "At end", h("select", { "data-k": "end" }, ["black", "hold", "loop"].map((m) => h("option", { value: m, selected: v.end === m }, m)))),
      h("label", { class: "f" }, "In point (s)", inp("in_point", v.in_point)),
      h("label", { class: "f" }, "Out point (s, empty = end)", inp("out_point", v.out_point)),
      h("label", { class: "f" }, "Fade in (s)", inp("fade_in", v.fade_in)),
      h("label", { class: "f" }, "Fade out (s)", inp("fade_out", v.fade_out))),
    h("label", { class: "f" }, "Pause points"), chips,
    h("div", { class: "row" }, h("div", { class: "grow" }, ppInput),
      h("button", { class: "small", onclick: () => { addPP(num(ppInput.value)); ppInput.value = ""; } }, "Add"),
      h("button", { class: "small", onclick: () => {
        if (!S.state || S.state.video !== v.id) return toast("this video is not playing on the projector", true);
        addPP(S.state.position);
      } }, "Add current position")),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: () => saveVideo(v, ed) }, "Save"),
      h("button", { onclick: () => { S.editVideo = null; renderVideos(); } }, "Cancel")),
    h("h3", {}, "Rename"),
    h("div", { class: "grid2" },
      h("label", { class: "f" }, "Video id (used by OSC /proj/play)", h("input", { "data-r": "new_id", value: v.id, autocapitalize: "off" })),
      h("label", { class: "f" }, "File name", h("input", { "data-r": "file", value: v.file, autocapitalize: "off" }))),
    h("div", { class: "row" }, h("button", { class: "small", onclick: () => renameVideo(v, ed) }, "Rename")));
  return ed;
}

async function saveVideo(v, ed) {
  const body = { pause_points: S.videoDraft.pause_points };
  ed.querySelectorAll("[data-k]").forEach((el) => {
    const k = el.dataset.k;
    if (k === "title" || k === "end") body[k] = el.value;
    else if (k === "out_point") body[k] = el.value.trim() === "" ? null : num(el.value);
    else body[k] = num(el.value) === null ? 0 : num(el.value);
  });
  const ok = await act(() => api("PATCH", `/api/videos/${encodeURIComponent(v.id)}`, body), "saved");
  if (ok) { S.editVideo = null; await loadVideos(); }
}

async function renameVideo(v, ed) {
  const body = {};
  ed.querySelectorAll("[data-r]").forEach((el) => { body[el.dataset.r] = el.value.trim(); });
  if (body.new_id === v.id && body.file === v.file) return;
  const r = await act(() => api("POST", `/api/videos/${encodeURIComponent(v.id)}/rename`, body), "renamed");
  if (r) { S.editVideo = r.id; S.videoDraft = null; await loadVideos(); }
}

// upload (XHR for upload progress; raw body streamed to the server)
$("upStart").addEventListener("click", startUpload);
$("upCancel").addEventListener("click", () => { if (S.upload) S.upload.xhr.abort(); });

async function startUpload() {
  const f = $("upFile").files[0];
  if (!f) return toast("choose a file first", true);
  const maxMb = (S.videos && S.videos.max_upload_mb) || (S.info && S.info.max_upload_mb) || 0;
  if (maxMb && f.size > maxMb * 1024 * 1024) return toast(`file too large (max ${maxMb} MB)`, true);
  const xhr = new XMLHttpRequest();
  const up = { xhr, t0: performance.now(), samples: [], lock: null };
  S.upload = up;
  try { if (navigator.wakeLock) up.lock = await navigator.wakeLock.request("screen"); } catch (e) { /* optional */ }
  const done = (msg, isErr) => {
    S.upload = null;
    if (up.lock) up.lock.release().catch(() => {});
    $("upStart").disabled = false;
    $("upCancel").classList.add("hidden");
    $("upMsg").innerHTML = "";
    $("upMsg").append(...(Array.isArray(msg) ? msg : [msg]));
    $("upMsg").className = "small " + (isErr ? "err" : "ok");
  };
  xhr.open("PUT", `/api/upload?name=${encodeURIComponent(f.name)}&overwrite=${$("upOverwrite").checked}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const now = performance.now();
    up.samples.push([now, e.loaded]);
    while (up.samples.length > 2 && now - up.samples[0][0] > 5000) up.samples.shift();
    const [t0, b0] = up.samples[0];
    const rate = now > t0 ? ((e.loaded - b0) / (now - t0)) * 1000 : 0;
    const pct = (e.loaded / e.total) * 100;
    $("upBar").style.width = pct + "%";
    $("upPct").textContent = `${pct.toFixed(1)}%  ${fmtBytes(e.loaded)} / ${fmtBytes(e.total)}`;
    $("upRate").textContent = rate ? fmtBytes(rate) + "/s" : "";
    $("upEta").textContent = rate ? "ETA " + fmtTime((e.total - e.loaded) / rate).replace(/\.\d$/, "") : "";
    if (e.loaded === e.total) $("upEta").textContent = "finishing (sync to disk)...";
  };
  xhr.onload = async () => {
    let data = null;
    try { data = JSON.parse(xhr.responseText); } catch (e) { /* not json */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      const parts = [h("div", {}, `Uploaded ${data.file} (${fmtBytes(data.size)})` + (data.video_id ? ` as video "${data.video_id}"` : ""))];
      for (const w of data.warnings || []) parts.push(h("div", { class: "warn" }, w));
      done(parts, false);
      $("upFile").value = "";
      await loadVideos();
    } else {
      done(`Upload failed (${xhr.status}): ${errText(data) || xhr.statusText}`, true);
      if (xhr.status === 401) showLogin();
    }
  };
  xhr.onerror = () => done("Upload failed: connection lost", true);
  xhr.onabort = () => done("Upload cancelled", true);
  $("upStart").disabled = true;
  $("upCancel").classList.remove("hidden");
  $("upProgressWrap").classList.remove("hidden");
  $("upBar").style.width = "0%";
  $("upMsg").textContent = "";
  xhr.send(f);
}
window.addEventListener("beforeunload", (e) => { if (S.upload) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ cues

function renderCues() {
  const box = $("cueList"); box.innerHTML = "";
  if (!S.show) return;
  const cues = S.show.cues;
  if (!cues.length) box.append(h("p", { class: "muted" }, "No cues yet."));
  cues.forEach((c, i) => {
    const v = S.show.videos[c.video];
    const ov = [];
    for (const k of ["start_at", "fade_in", "fade_out", "end"]) if (c[k] !== null && c[k] !== undefined) ov.push(`${k.replace("_", " ")}: ${c[k]}`);
    if (c.pause_points) ov.push(`pauses: ${c.pause_points.map(fmtTime).join(", ") || "none"}`);
    const item = h("div", { class: "item" },
      h("div", { class: "row" },
        h("div", { class: "row", style: "gap:4px" },
          h("button", { class: "small", disabled: i === 0, title: "move up", onclick: () => moveCue(i, -1) }, "▲"),
          h("button", { class: "small", disabled: i === cues.length - 1, title: "move down", onclick: () => moveCue(i, 1) }, "▼")),
        h("div", { class: "grow" },
          h("div", { class: "title" }, `#${i + 1}  `, h("span", { class: "muted" }, `id ${c.id}`), "  ", c.name || "(unnamed)"),
          h("div", { class: "sub" }, v ? `${v.title || c.video} [${c.video}]` : `missing video ${c.video}`, ov.length ? " - " + ov.join(" - ") : "")),
        h("button", { class: "small", onclick: () => { S.editCue = S.editCue === c.id ? null : c.id; renderCues(); } }, S.editCue === c.id ? "Close" : "Edit")));
    if (S.editCue === c.id) item.append(cueEditor(c));
    box.append(item);
  });
}

function cueEditor(c) {
  const v = S.show.videos[c.video] || {};
  const val = (x) => (x === null || x === undefined ? "" : x);
  const field = (label, key, value, ph) => h("label", { class: "f" }, label,
    h("input", { "data-k": key, value: val(value), placeholder: ph || "", inputmode: ["id", "name"].includes(key) ? null : "decimal" }));
  const ed = h("div", { class: "editor" },
    h("div", { class: "grid2" },
      field("Cue id", "id", c.id),
      field("Name", "name", c.name),
      h("label", { class: "f" }, "Video", h("select", { "data-k": "video" },
        Object.entries(S.show.videos).map(([id, vv]) => h("option", { value: id, selected: id === c.video }, `${vv.title || id} [${id}]`)))),
      field("Start at (s)", "start_at", c.start_at, `video in point: ${v.in_point ?? 0}`),
      field("Fade in (s)", "fade_in", c.fade_in, `video: ${v.fade_in ?? "-"}`),
      field("Fade out (s)", "fade_out", c.fade_out, `video: ${v.fade_out ?? "-"}`),
      h("label", { class: "f" }, "At end", h("select", { "data-k": "end" },
        h("option", { value: "", selected: !c.end }, `inherit (${v.end || "black"})`),
        ["black", "hold", "loop"].map((m) => h("option", { value: m, selected: c.end === m }, m)))),
      field("Pause points (s, comma sep.)", "pause_points", c.pause_points ? c.pause_points.join(", ") : "",
        `inherit: ${(v.pause_points || []).join(", ") || "none"}`)),
    h("label", { class: "inline small muted" }, h("input", { type: "checkbox", "data-k": "pp_none", checked: !!(c.pause_points && !c.pause_points.length) }),
      "no pause points for this cue (override video's)"),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: () => saveCue(c, ed) }, "Save"),
      h("button", { onclick: () => { S.editCue = null; renderCues(); } }, "Cancel"),
      h("span", { class: "grow" }),
      h("button", { class: "danger", onclick: () => deleteCue(c) }, "Delete cue")));
  return ed;
}

async function saveCue(c, ed) {
  const body = {};
  let ppNone = false;
  ed.querySelectorAll("[data-k]").forEach((el) => {
    const k = el.dataset.k;
    if (k === "pp_none") { ppNone = el.checked; return; }
    if (["id", "name", "video"].includes(k)) body[k] = el.value;
    else if (k === "end") body[k] = el.value || null;
    else if (k === "pause_points") body[k] = el.value.trim() === "" ? null : el.value;
    else body[k] = el.value.trim() === "" ? null : num(el.value);
  });
  if (ppNone) body.pause_points = [];
  const r = await act(() => api("PUT", `/api/cues/${encodeURIComponent(c.id)}`, body), "cue saved");
  if (r) { S.editCue = null; await loadShow(); renderCues(); }
}

async function deleteCue(c) {
  if (!confirm(`Delete cue ${c.id} "${c.name}"?`)) return;
  const r = await act(() => api("DELETE", `/api/cues/${encodeURIComponent(c.id)}`), "cue deleted");
  if (r) { S.editCue = null; await loadShow(); renderCues(); }
}

async function moveCue(i, dir) {
  const ids = S.show.cues.map((c) => c.id);
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  const r = await act(() => api("POST", "/api/cues/reorder", { order: ids }));
  if (r) { await loadShow(); renderCues(); }
}

$("cueAdd").addEventListener("click", async () => {
  if (!S.show || !Object.keys(S.show.videos).length) return toast("upload a video first", true);
  const lastVid = S.show.cues.length ? S.show.cues[S.show.cues.length - 1].video : Object.keys(S.show.videos)[0];
  const r = await act(() => api("POST", "/api/cues", { name: "New cue", video: lastVid }));
  if (r) { S.editCue = r.cue.id; await loadShow(); renderCues(); }
});

// ------------------------------------------------------------------ system

function kv(el, pairs) {
  el.innerHTML = "";
  for (const [k, v, cls] of pairs) {
    if (v === undefined) continue;
    el.append(h("span", {}, k), h("span", { class: cls || "" }, v === null || v === "" ? "-" : String(v)));
  }
}

async function loadSystem() {
  let s;
  try { s = await api("GET", "/api/system"); } catch (e) { return; }
  const n = s.net || {};
  kv($("sysNet"), [
    ["State", n.state, n.state === "connected" ? "ok" : n.state === "hotspot" ? "" : "err"],
    ["SSID", n.ssid], ["IP", n.ip],
    n.state === "hotspot" ? ["Hotspot", `${n.hotspot_ssid || "-"}  ${n.hotspot_ip || ""}`] : ["", undefined],
    n.state === "connecting" ? ["Attempt", `${n.attempt ?? "-"} (next retry in ${n.next_retry_s ?? "-"} s)`] : ["", undefined],
    ["Updated", n.updated ? fmtAge(n.updated) : (n.error || "-")],
  ]);
  const t = s.tunnel || {};
  kv($("sysTunnel"), [["State", t.state, t.state === "up" ? "ok" : ""], ["Remote", t.remote], ["Updated", t.updated ? fmtAge(t.updated) : (t.error || "-")]]);
  const i = s.info || {};
  const th = i.throttled;
  kv($("sysDevice"), [
    ["Model", i.model || "-"],
    ["Version", S.info ? S.info.version : "-"],
    ["Disk free", i.disk ? `${fmtBytes(i.disk.free)} of ${fmtBytes(i.disk.total)}` : "-"],
    ["CPU temp", i.cpu_temp_c !== null && i.cpu_temp_c !== undefined ? i.cpu_temp_c + " °C" : "-"],
    ["Uptime", i.uptime_s ? fmtDur(i.uptime_s) : "-"],
    ["Power", th ? (th.undervoltage_now ? "UNDERVOLTAGE NOW" : th.undervoltage_since_boot ? "undervoltage occurred since boot" : "ok") +
      (th.throttled_now ? ", throttled" : "") : undefined, th && (th.undervoltage_now || th.undervoltage_since_boot) ? "err" : "ok"],
  ]);
  const pe = s.power_enabled || {};
  $("pwInfo").textContent = pe.reboot || pe.shutdown ? "" : "Power buttons are disabled in dev mode (not running on the Pi image).";
  renderTransports();
}

function renderTransports() {
  const tr = (S.state && S.state.transports) || {};
  kv($("sysTransports"), Object.entries(tr).map(([k, v]) => [k.toUpperCase(), v]));
}
$("sysRefresh").addEventListener("click", () => { loadSystem(); loadWifi(); loadBackup(); });
setInterval(() => { if (S.tab === "system" && !document.hidden) loadSystem(); }, 5000);

function netList(d) {
  if (Array.isArray(d)) return d;
  if (d && Array.isArray(d.networks)) return d.networks;
  return [];
}

async function loadWifi() {
  const box = $("wifiKnown");
  let d;
  try { d = await api("GET", "/api/wifi"); } catch (e) { box.textContent = "WiFi manager: " + e.message; box.className = "err small"; return; }
  box.className = ""; box.innerHTML = "";
  const nets = netList(d);
  if (!nets.length) box.append(h("p", { class: "muted small" }, "No known networks."));
  for (const n of nets) {
    const ssid = typeof n === "string" ? n : n.ssid;
    const prio = n.priority ?? n.prio;
    box.append(h("div", { class: "item row" },
      h("div", { class: "grow" }, h("span", { class: "title" }, ssid), n.active ? h("span", { class: "tag cur" }, "connected") : null,
        prio !== undefined ? h("span", { class: "muted small" }, `  priority ${prio}`) : null,
        n.security === "eap" ? h("span", { class: "muted small" }, `  · enterprise${n.identity ? " (" + n.identity + ")" : ""}`) : null,
        n.security === "open" ? h("span", { class: "muted small" }, "  · open") : null),
      h("button", { class: "small danger", onclick: () => {
        if (confirm(`Forget network "${ssid}"?`)) act(async () => { await api("DELETE", "/api/wifi/" + encodeURIComponent(ssid)); await loadWifi(); }, "removed");
      } }, "Remove")));
  }
}

function wifiSecChanged() {
  const sec = $("wifiSec").value;
  document.querySelectorAll("#wifiForm .eap-only").forEach((el) => el.classList.toggle("hidden", sec !== "eap"));
  document.querySelector("#wifiForm .sec-pw").classList.toggle("hidden", sec === "open");
}
$("wifiSec").addEventListener("change", wifiSecChanged);
wifiSecChanged();

$("wifiForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const security = $("wifiSec").value;
  const body = { ssid: $("wifiSsid").value.trim(), security, priority: $("wifiPrio").value };
  if (security === "psk") body.psk = $("wifiPsk").value;
  if (security === "eap") Object.assign(body, {
    identity: $("wifiId").value.trim(), password: $("wifiPsk").value, method: $("wifiEap").value,
    phase2: $("wifiPhase2").value, anonymous_identity: $("wifiAnon").value.trim(),
    domain_suffix: $("wifiDomain").value.trim(), ca_cert: $("wifiCa").value.trim(),
  });
  const r = await act(() => api("POST", "/api/wifi", body), "network added");
  if (r) { for (const id of ["wifiSsid", "wifiPsk", "wifiId", "wifiAnon", "wifiDomain", "wifiCa"]) $(id).value = ""; loadWifi(); }
});

$("wifiScan").addEventListener("click", async () => {
  $("wifiMsg").textContent = "scanning...";
  const box = $("wifiScanList"); box.innerHTML = "";
  try {
    const nets = netList(await api("POST", "/api/wifi/scan"));
    $("wifiMsg").textContent = `${nets.length} network(s) visible`;
    nets.sort((a, b) => (b.signal || 0) - (a.signal || 0));
    for (const n of nets) {
      box.append(h("div", { class: "item row" },
        h("div", { class: "grow" }, h("span", { class: "title" }, n.ssid || "(hidden)"),
          h("span", { class: "muted small" }, `  ${n.signal ?? "?"}%  ${n.security || "open"}`), n.known ? h("span", { class: "tag" }, "known") : null),
        n.ssid ? h("button", { class: "small", onclick: () => { $("wifiSsid").value = n.ssid; if (/802\.1X|EAP/i.test(n.security || "")) { $("wifiSec").value = "eap"; wifiSecChanged(); } $("wifiPsk").focus(); } }, "Use") : null));
    }
  } catch (e) { $("wifiMsg").textContent = e.message; }
});

$("wifiRetry").addEventListener("click", () => act(() => api("POST", "/api/wifi/retry"), "retrying known networks"));

// backup
function renderBackup(b) {
  const lb = b.last_backup, lr = b.last_restore;
  kv($("bkInfo"), [
    ["Server", b.enabled ? (b.configured ? b.remote : "not configured") : "backup disabled", b.enabled && b.configured ? "" : "err"],
    ["Last backup", lb ? `${lb.state} - ${fmtAge(lb.finished)}` : "never", lb && lb.state !== "done" ? "err" : ""],
    ["Last restore", lr ? `${lr.state} - ${fmtAge(lr.finished)}` : "never"],
    ["Job", b.job ? `${b.job}: ${b.state}${b.phase ? " (" + b.phase + ")" : ""}${b.message ? " - " + b.message : ""}` : "idle",
      b.state === "error" ? "err" : b.state === "done" ? "ok" : ""],
  ]);
  const running = !!b.running;
  $("bkProgressWrap").classList.toggle("hidden", !running);
  $("bkCancel").classList.toggle("hidden", !running);
  $("bkRun").disabled = running || !b.enabled || !b.configured;
  $("bkRestore").disabled = running || !b.enabled || !b.configured;
  $("bkList").disabled = !b.enabled || !b.configured;
  if (running) {
    $("bkBar").style.width = (b.percent || 0) + "%";
    $("bkPct").textContent = `${b.phase || ""} ${b.percent || 0}%  ${fmtBytes(b.bytes || 0)}`;
    $("bkRate").textContent = b.rate || "";
    $("bkEta").textContent = b.eta ? "ETA " + b.eta : "";
  }
  $("bkLog").textContent = (b.log || []).slice(-8).join("\n");
  clearTimeout(S.bkPoll);
  if (running) S.bkPoll = setTimeout(loadBackup, 1000);
  else if (b.job === "restore" && b.state === "done" && S.lastBkState === "running") loadShow();
  S.lastBkState = b.state;
}

async function loadBackup() {
  try { renderBackup(await api("GET", "/api/backup")); } catch (e) { /* ignore */ }
}

$("bkRun").addEventListener("click", async () => {
  const r = await act(() => api("POST", "/api/backup/run"), "backup started");
  if (r) renderBackup(r);
});
$("bkCancel").addEventListener("click", () => act(() => api("POST", "/api/backup/cancel"), "cancelling"));
$("bkList").addEventListener("click", async () => {
  const r = await act(() => api("GET", "/api/backup/snapshots"));
  if (!r) return;
  const sel = $("bkSnap");
  sel.innerHTML = "";
  sel.append(h("option", { value: "latest" }, "latest backup"));
  for (const s of r.snapshots) sel.append(h("option", { value: s.name }, `${new Date(s.timestamp * 1000).toLocaleString()}  (${fmtBytes(s.size)})`));
  toast(`${r.snapshots.length} snapshot(s)`);
});
$("bkRestore").addEventListener("click", async () => {
  const snap = $("bkSnap").value, media = $("bkMedia").checked;
  if (!confirm(`Restore ${snap === "latest" ? "the latest backup" : snap}${media ? " including video files" : ""}?\n\n` +
    "The current show.json will be replaced (a copy is kept as show.before-restore.json).")) return;
  const r = await act(() => api("POST", "/api/backup/restore", { snapshot: snap, media }), "restore started");
  if (r) renderBackup(r);
});

// show import
$("showImport").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  let data;
  try { data = JSON.parse(await f.text()); } catch (e) { return toast("not valid JSON: " + e.message, true); }
  const nv = data.videos ? Object.keys(data.videos).length : 0, nc = data.cues ? data.cues.length : 0;
  if (!confirm(`Replace the current show with ${f.name} (${nv} videos, ${nc} cues)?`)) return;
  const r = await act(() => api("PUT", "/api/show", data), "show imported");
  if (r) await loadShow();
});

// power
for (const [id, what, txt] of [["pwReboot", "reboot", "Reboot the projector now?"], ["pwShutdown", "shutdown", "Shut down the projector now?\nIt must be power-cycled to start again."]]) {
  $(id).addEventListener("click", async () => {
    if (!confirm(txt)) return;
    const r = await act(() => api("POST", "/api/system/" + what, { confirm: true }));
    if (r) toast(r.dry_run ? r.message : `${what} initiated`);
  });
}

$("logout").addEventListener("click", async () => {
  await act(() => api("POST", "/api/logout"));
  location.reload();
});

// OSC reference
$("oscRef").append(...OSC_REF.map(([a, args, eff]) => h("tr", {}, h("td", { class: "mono" }, a), h("td", { class: "mono" }, args), h("td", {}, eff))));

// ------------------------------------------------------------------ startup

let started = false;
async function start() {
  if (started) return;
  started = true;
  try { S.info = await api("GET", "/api/info"); } catch (e) { started = false; return; }
  $("devName").textContent = S.info.device;
  document.title = S.info.device + " - Projektor";
  $("logout").classList.toggle("hidden", !S.info.auth_required);
  connectWS();
  await loadShow();
  setTab((location.hash || "#live").slice(1));
}

(async function init() {
  try {
    const a = await (await fetch("/api/auth", { credentials: "same-origin" })).json();
    S.auth = a;
    $("devName").textContent = a.device || "Projektor";
    if (a.auth_required && !a.authenticated) return showLogin();
  } catch (e) { toast("cannot reach projector", true); }
  start();
})();

// ------------------------------------------------------------------ beamer (HDMI-CEC)
async function loadBeamer() {
  let r;
  try { r = await api("GET", "/api/beamer"); } catch (e) { return; }
  const el = $("beamerState");
  el.textContent = r.enabled ? r.status : "disabled";
  el.className = r.status === "on" ? "ok" : r.status === "unavailable" ? "err" : "";
}
async function beamerPower(on) {
  const r = await act(() => api("POST", "/api/beamer/power", { on }), on ? "beamer on sent" : "beamer standby sent");
  if (r) setTimeout(loadBeamer, 3000);
}
$("beamerOn").addEventListener("click", () => beamerPower(true));
$("beamerOff").addEventListener("click", () => { if (confirm("Put the beamer into standby?")) beamerPower(false); });
$("beamerKeys").append(...["up", "down", "left", "right", "select", "back", "home", "menu", "volume-up", "volume-down", "mute"]
  .map((k) => h("button", { class: "small", onclick: () => act(() => api("POST", "/api/beamer/key", { key: k })) }, k)));
loadBeamer();
