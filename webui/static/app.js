"use strict";
/* hermes webui client.
 *
 * Two rules shape this file.
 *
 * 1. A turn belongs to the SERVER, not to this page. The running turn's id and
 *    the last event sequence rendered both live in localStorage, and on load the
 *    page ASKS what happened while it was away. Reattaching is the normal path,
 *    not the error path.
 *
 * 2. A turn is a sequence of SEGMENTS, each either a thought or output. Switching
 *    kind closes the current segment and opens a new one. Rendering the two into
 *    one bubble — which an earlier version did — turns the transcript into the
 *    model's internal monologue glued to its answer, and the reader cannot tell
 *    which is which.
 */

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const LS_VIEW = "hermes.view";      // PER PROJECT — see viewKey(); a reload reopens it
const LS_OPEN = "hermes.open";      // which activity groups the reader had open
const LS_EP = "hermes.endpoint";    // last-used endpoint: the DEFAULT new sessions start on
const LS_SESS_EP = "hermes.sessionEndpoints";   // session_id -> endpoint, so the picker
                        // still shows a conversation's model after a reload
const LS_THEME = "hermes.theme";    // "light" | "dark"; unset = dark (the original look)

// Keyed by session + the group's index in the transcript, which is stable for a
// given conversation: reload it, switch away and back, and the rows you opened
// are still open. Upstream persists this per chat and per turn for the same
// reason -- being made to re-open the same trace every visit is what makes a
// disclosure feel like it is fighting you.
function openState() {
  try { return JSON.parse(localStorage.getItem(LS_OPEN) || "{}"); } catch (_) { return {}; }
}
// Keyed by session so one conversation's open groups do not decide another's.
// `S.sessionId` used to be set only by `openSession`, so every conversation
// started from 新会话 — and every boot-time reattach — shared one "-" bucket:
// opening group 0 in one of them pre-opened group 0 in all the others.
function openKey(idx) { return `${S.sessionId || "-"}#${idx}`; }
function isOpen(idx) { return openState()[openKey(idx)] === 1; }
function setOpen(idx, on) {
  try {
    const m = openState();
    if (on) m[openKey(idx)] = 1; else delete m[openKey(idx)];
    // Bounded: one entry per turn per session forever would grow without limit.
    const keys = Object.keys(m);
    if (keys.length > 400) keys.slice(0, keys.length - 400).forEach((k) => delete m[k]);
    localStorage.setItem(LS_OPEN, JSON.stringify(m));
  } catch (_) { /* private mode: the state is a convenience, not a requirement */ }
}

// Open links in a new tab, and never hand the opener over with them.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});
const renderMD = (src) => {
  const box = document.createElement("div");
  box.innerHTML = DOMPurify.sanitize(
    marked.parse(src || "", { gfm: true, breaks: true }),
  );
  return box.innerHTML;
};

/* ---------- mermaid ---------- */
// The code-understanding profile asks the model for diagrams in so many words
// ("被要求画流程图/架构图时，直接用 mermaid 给出")， so they arrive on this app's
// hot path — and until now they were shown as their own source, which is the
// one thing a diagram must not be.
//
// Loaded on FIRST USE rather than with the page. The bundle is 3.4 MB, larger
// than everything else this app serves put together, and most conversations
// never contain a diagram; the homepage was taken from 820 ms to 28 ms by
// keeping work off the first paint and this would hand it all back.
const mermaidTheme = () =>
  document.documentElement.dataset.theme === "light" ? "default" : "dark";

let mermaidReady = null;
function loadMermaid() {
  if (mermaidReady) return mermaidReady;
  mermaidReady = new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = "/static/vendor/mermaid.min.js";
    tag.onload = () => resolve(window.mermaid);
    tag.onerror = () => reject(new Error("mermaid.min.js"));
    document.head.appendChild(tag);
  }).then((m) => {
    m.initialize({
      startOnLoad: false,        // we call render ourselves, on finished text
      // The diagram is written by a model reading a corpus, which is not a
      // trusted author: `strict` escapes label HTML and leaves click handlers
      // off. Everything else here already treats model output that way —
      // DOMPurify sanitises the markdown around it.
      securityLevel: "strict",
      theme: mermaidTheme(),
      // SVG <text> labels, not HTML ones, and this is not cosmetic. mermaid
      // sizes an HTML label by measuring a real element it appends to the
      // document — where OUR stylesheet applies. The label's <p> is a block,
      // so it measures the full body width, and every node comes out that
      // wide: one 33-node flowchart laid out at 17482×48834 instead of
      // 2332×3537, which is what the same source gives in a stylesheet-free
      // iframe. Squeezed back into the column it was an unreadable grey smear.
      // `<br/>` still breaks lines — mermaid splits SVG labels into tspans.
      //
      // BOTH keys, and the TOP-LEVEL one is the one that works. Setting only
      // `flowchart.htmlLabels` changes nothing in mermaid 11 — measured on the
      // live page: flowchart-only kept the foreignObject and the 17443x48754
      // layout, top-level alone gave 2332x3537. Config is also sticky across
      // `initialize` calls, so an experiment that sets the top-level flag once
      // makes every later flowchart-only call look like it worked.
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      // Measure with the font we will actually PAINT with. SVG <text>
      // inherits font-family from the page, while mermaid sizes every node by
      // measuring the label itself — hand it `inherit` (a string it treats as
      // a font name) or leave it on its own default and the two disagree, so
      // labels render wider than the boxes drawn for them and get clipped at
      // the svg's edge.
      fontFamily: getComputedStyle(document.body).fontFamily,
    });
    return m;
  });
  return mermaidReady;
}

// Draw at the size the diagram wants and let the box scroll, rather than
// fitting the width. mermaid emits `width="100%"`, which on a 2332px-wide
// flowchart in a 760px column is a 0.31 scale — the lines survive, the labels
// do not. A reader can scroll; a reader cannot un-shrink 8px text.
function sizeToContent(fig) {
  const svg = fig.querySelector("svg");
  if (!svg) return;
  const vb = (svg.getAttribute("viewBox") || "").split(/[ ,]+/).map(Number);
  if (vb.length !== 4 || !vb[2]) return;
  svg.setAttribute("width", vb[2]);
  svg.setAttribute("height", vb[3]);
  svg.style.maxWidth = "none";
}

let mermaidSeq = 0;
// Render every ```mermaid block under `root`, in place.
//
// Called from finalizeSeg only, never from the streaming render: a fence that
// is still arriving is a parse error, and re-drawing on every frame would burn
// the diagram down and rebuild it sixty times a second. Streaming shows the
// source; it becomes a picture when the answer is complete.
async function renderMermaid(root) {
  if (!root) return;
  const blocks = [...root.querySelectorAll("pre > code.language-mermaid")];
  if (!blocks.length) return;
  let m;
  try {
    m = await loadMermaid();
  } catch (_) {
    return;   // no bundle: the source stays on screen, which is the fallback
  }
  for (const code of blocks) {
    const pre = code.parentElement;
    if (!pre || !pre.isConnected) continue;
    const src = code.textContent || "";
    let svg;
    try {
      ({ svg } = await m.render(`mmd-${++mermaidSeq}`, src));
    } catch (e) {
      // A diagram the model wrote wrong must not eat the answer around it.
      // Keep the source exactly where it was and say why it is still source.
      pre.classList.add("mermaid-failed");
      if (!pre.nextElementSibling?.classList.contains("mermaid-error")) {
        const note = el("div", "mermaid-error", `图表语法有误：${e?.message || e}`);
        pre.after(note);
      }
      continue;
    }
    const fig = el("div", "mermaid-figure");
    fig.dataset.src = src;   // kept so a theme switch can redraw it
    fig.innerHTML = svg;
    sizeToContent(fig);
    pre.replaceWith(fig);
  }
  scroll();
}

// mermaid bakes its colours into the SVG at render time, so a theme switch has
// to redraw rather than restyle. Only diagrams already on screen, and only if
// the bundle was ever loaded — this must not pull 3.4 MB for a theme click.
// Hung off the attribute, not off the toggle's click. That click handler is
// duplicated verbatim in home.js and a test holds the two byte-identical
// (tests/js/theme_toggle.mjs) — the homepage has no diagrams and should not
// carry a call to this. Watching `data-theme` also covers anything else that
// ever changes it, including the pre-paint script in <head>.
if (typeof MutationObserver === "function") {
  new MutationObserver(() => restyleMermaid()).observe(
    document.documentElement, { attributes: true, attributeFilter: ["data-theme"] },
  );
}

async function restyleMermaid() {
  const figs = [...document.querySelectorAll(".mermaid-figure[data-src]")];
  if (!figs.length || !mermaidReady) return;
  const m = await mermaidReady.catch(() => null);
  if (!m) return;
  m.initialize({
    startOnLoad: false, securityLevel: "strict",
    theme: mermaidTheme(),
    htmlLabels: false, flowchart: { htmlLabels: false },
    fontFamily: getComputedStyle(document.body).fontFamily,
  });
  for (const fig of figs) {
    try {
      const { svg } = await m.render(`mmd-${++mermaidSeq}`, fig.dataset.src);
      fig.innerHTML = svg;
      sizeToContent(fig);
    } catch (_) { /* keep the last good drawing */ }
  }
}

const S = {
  streamId: null,
  lastSeq: 0,
  ess: new Map(),       // streamId -> its live EventSource. ONE per turn, not one
                        // per view: two endpoints mean two turns can run at once,
                        // and a single slot made watching session B freeze session A
                        // (attach closed A's feed; switching back orphaned B).
  busy: false,
  owns: false,          // the conversation ON SCREEN has a live turn; decided in
                        // setBusy. Send/stop gate on this, never on `busy`.
  seg: null,          // { kind: "think"|"out", body, text, refs }
  tools: new Map(),
  approvalTimer: null,
  awaitingPerm: false,
  skipUserEcho: false,   // we drew this turn's prompt optimistically
  activity: null,        // the current turn's one activity disclosure
  actIndex: 0,           // its position in the transcript, for the open-state key
  turnTop: null,         // this turn's FIRST answer bubble, so the activity
                         // disclosure can be put in front of it — see
                         // activityGroup. Cleared wherever a turn begins.
  sessionId: null,
  switching: null,      // a history read in flight; sending must wait for it
  viewGen: 0,           // bumped whenever the view changes (openSession/newSession);
                        // an await that returns to a different gen must not touch it
  pendingNew: false,    // "new session" clicked; the ACP move happens on send
  ownStream: null,      // the stream THIS view started, before it has an id
  sessionRows: [],      // last list from the server, so a click can repaint now
  endpoints: [],        // what /api/status advertises: {key,label,model,maxConcurrent,running}
  endpoint: null,       // which endpoint THIS session's next send names — read from
  sessionEp: loadSessionEp(),  //   sessionEp[sessionId]; sessions WITHOUT a choice (new, other
                        //   tab) start on the last-used default. A turn already running
                        //   keeps its own endpoint; switching decides the next turn only.
  // The PROJECT this page serves. Two pages, deepwiki style: the home (/) is
  // the card grid and a project lives at /<key>/ — this chat page reads its
  // key off the URL and never changes it. Switching project = going back to
  // the home page and opening another card, a navigation, not a widget state.
  profile: (location.pathname.replace(/\/+$/, "").split("/").pop() || "").trim(),
  defaultProfile: null,  // the server's first profile; sessions without a pin file under it,
  // (maxConcurrent/running were flat scalars from the single-endpoint days;
  // per-endpoint numbers live inside S.endpoints now.)
  // "a turn this view does not own is running, and the pool is full" — decided
  // once in setBusy so the button and the status line cannot contradict.
  blockedElsewhere: false,
  // Which sessions are streaming, and on what stream: { sessionId: streamId }.
  // A pair of scalars before, because the server could only ever run one turn.
  // It can run several now, so returning to the second live conversation has to
  // find ITS stream, not the one that happened to be recorded last.
  streaming: {},
  watchTimer: null,     // refreshes the sidebar while a turn runs somewhere else
  watchPeriod: null,    // ms; 3s while anything streams, 15s idle
  stopping: false,
  startedAt: 0,
  timer: null,
};

/* ---------- just enough state to recover ---------- */
// A reload remembers WHICH CONVERSATION was on screen, not a stream cursor.
// The cursor version resumed a stream after `lastSeq` into a page the reload
// had just emptied — so everything already painted was gone — and it cleared
// itself only if `forget()` ran at exactly the right moment. When it did not, a
// finished stream stayed saved and every later reload attached to it, drew
// nothing, and highlighted a row over an empty transcript. Reopening the
// session repaints from the store and replays a live turn from the top, which
// is the path a sidebar click already takes.
// SCOPED TO THE PROJECT, because the page is. One key per browser meant the
// conversation last read ANYWHERE came back on whichever project page opened
// next: the header said 代码理解·autumn-rs, the sidebar (correctly filtered)
// was empty, and the transcript was a 佛典 conversation. Reported from
// production with a screenshot. The server no longer lets a send from that
// screen re-pin the conversation, but the screen should not happen.
function viewKey() { return S.profile ? `${LS_VIEW}.${S.profile}` : LS_VIEW; }
function rememberView(sid) {
  try {
    if (sid) localStorage.setItem(viewKey(), sid);
    else localStorage.removeItem(viewKey());
  } catch (_) { /* private mode: a reload opens on 新的对话 */ }
}
function recallView() {
  try { return localStorage.getItem(viewKey()); } catch (_) { return null; }
}
// Does this conversation belong on THIS page? The sidebar's filter, as a
// predicate, so "what this page may show" has one definition instead of two —
// the drift between them is what put another project's transcript on screen:
// the recall checked the UNFILTERED row list, the sidebar filtered at render.
function belongsHere(id) {
  if (!S.profile) return true;                 // no project: the page shows everything
  const row = (S.sessionRows || []).find((r) => r.id === id);
  if (!row) return false;                      // unknown here is not ours to open
  return (row.profile || S.defaultProfile) === S.profile;
}

/* ---------- chrome ---------- */
function status(text) {
  $("#run-status").textContent = text;
  setPendingText(text);          // the tail row mirrors it, where the eye is
}

/* ---------- the endpoint picker ---------- */
// The server is the source of truth for WHICH endpoints exist; this page only
// decides which of them the next send names. The choice persists across
// reloads, and a saved key that the server no longer advertises falls back to
// the default rather than failing every send until the reader notices.
function savedEndpoint() {
  try { return localStorage.getItem(LS_EP); } catch (_) { return null; }
}
function saveEndpoint(key) {
  try {
    if (key) localStorage.setItem(LS_EP, key);
    else localStorage.removeItem(LS_EP);
  } catch (_) { /* private mode: the choice degrades to the default each load */ }
}

function endpointFor(sid) {
  // What session `sid` answers with: its own recorded choice, else the
  // last-used default. Raw — callers validate against what the server
  // advertises, because this can run before the first endpoint list lands.
  return S.sessionEp[sid || ""] || savedEndpoint() || null;
}

function noteSessionEndpoint(sid) {
  // Pin the pending choice onto a session the moment it acquires an id, so
  // the picker follows the conversation instead of the tab.
  if (sid && S.endpoint) {
    S.sessionEp[sid] = S.endpoint;
    saveSessionEp();
  }
}

function saveSessionEp() {
  // The map must survive a reload — the whole complaint it answers is "the
  // picker forgot which model this session used". Cap at 200 ids, oldest
  // inserted first; insertion order is the only order a plain object gives.
  try {
    const keys = Object.keys(S.sessionEp);
    for (const k of keys.slice(0, Math.max(0, keys.length - 200))) delete S.sessionEp[k];
    localStorage.setItem(LS_SESS_EP, JSON.stringify(S.sessionEp));
  } catch (_) { /* private mode: the picker degrades to the last-used default */ }
}

function loadSessionEp() {
  try { return JSON.parse(localStorage.getItem(LS_SESS_EP)) || {}; }
  catch (_) { return {}; }
}

function setEndpoints(list, defaultKey) {
  // Merge, don't replace: /api/sessions polls every few seconds WITH running
  // counts and /api/status answers once without them — replacing wholesale
  // would zero the other's numbers each time it landed. The fresh list wins
  // for what it advertises; running counts only carry over when it stayed
  // silent on them.
  const fresh = (list || []).map((e) => ({ ...e }));
  for (const e of fresh) {
    if (e.running === undefined) {
      const old = S.endpoints.find((x) => x.key === e.key);
      if (old) e.running = old.running;
    }
  }
  S.endpoints = fresh;

  // Resolve the choice ONCE here, so both send() and setBusy() read one value.
  // The base is the OPEN SESSION's choice, not a tab-wide one.
  const keys = new Set(S.endpoints.map((e) => e.key));
  if (!S.endpoint || !keys.has(S.endpoint)) {
    const want = endpointFor(S.sessionId);
    S.endpoint = (want && keys.has(want)) ? want
      : (defaultKey && keys.has(defaultKey)) ? defaultKey
      : (S.endpoints[0] ? S.endpoints[0].key : null);
    saveEndpoint(S.endpoint);
  }
  renderEndpoints();
  setBusy(S.busy);   // the capacity facts may have changed under the choice
}

function renderEndpoints() {
  const sel = $("#endpoint");
  const badge = $("#model-badge");
  if (!S.endpoints.length) {
    badge.textContent = "模型未配置";
    badge.hidden = false; sel.hidden = true;
    return;
  }
  // Always a select, one entry or several — the small-font row below the
  // composer box (the "Fast ⌄" slot): what will answer is visible and
  // changeable, not a passive label.
  badge.hidden = true; sel.hidden = false;
  // Rebuild only when the set changed: replaceChildren on every poll would
  // close the open dropdown under a reader's hand.
  const sig = S.endpoints.map((e) => e.key).join(",");
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.replaceChildren(...S.endpoints.map((e) => {
      const o = el("option");
      o.value = e.key;
      o.textContent = e.label;
      return o;
    }));
  }
  sel.value = S.endpoint || "";
}

function pickEndpoint(key) {
  if (!key || key === S.endpoint) return;
  S.endpoint = key;
  // A per-conversation property, not a tab-wide one: the next turn in THIS
  // session answers with `key`, other sessions keep theirs. It also becomes
  // the last-used default, which seeds sessions without a choice.
  S.sessionEp[S.sessionId || ""] = key;
  saveSessionEp();
  saveEndpoint(key);
  const e = S.endpoints.find((x) => x.key === key);
  status(`本会话下一个回复使用 ${e ? e.label : key}`);
  setBusy(S.busy);   // the new endpoint's occupancy decides the composer, not the old one's
}

const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
let spinFrame = 0;

function tick() {
  // The spinner is the only thing on screen that says "still working" during a
  // long tool call, where no token arrives for a minute at a time. A static
  // status line reads as a hang.
  const ch = S.busy ? SPIN[spinFrame++ % SPIN.length] : "";
  $("#spin").textContent = ch;
  const ps = $("#pending .p-spin");
  if (ps) ps.textContent = ch;
  // Each running tool times itself from when its row appeared, not from the
  // turn start -- "this search has been going 40s" is the useful number.
  document.querySelectorAll("[data-since]").forEach((n) => {
    const ms = Date.now() - Number(n.dataset.since);
    const sec = Math.floor(ms / 1000);
    n.textContent = sec < 60 ? ` ${sec}s` : ` ${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, "0")}s`;
  });
}

function tickSlow() {
  if (!S.startedAt) { $("#elapsed").textContent = ""; return; }
  const s = Math.floor((Date.now() - S.startedAt) / 1000);
  $("#elapsed").textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

function scroll() {
  const m = $("#messages");
  // Follow the tail only if the reader is already there. Yanking the viewport
  // away from someone reading back is worse than a missed autoscroll.
  if (m.scrollHeight - m.scrollTop - m.clientHeight < 140) m.scrollTop = m.scrollHeight;
}

/* ---------- messages ---------- */
function showFresh() {
  // A brand-new conversation looks nothing like one with history: the greeting
  // and the composer sit together in the middle, so it is obvious at a glance
  // that nothing has been said yet and there is no context behind it.
  const m = $("#messages");
  m.textContent = "";
  const hero = el("div", "fresh-hero");
  hero.append(
    el("div", "fresh-title", "新的对话"),
    el("div", "fresh-sub", "还没有任何上下文 — 问点什么开始吧"),
  );
  m.appendChild(hero);
  $(".chat").classList.add("fresh");
  // The standing placeholder is a keyboard reference — right above a
  // transcript, wrong as the one line inside an otherwise empty screen, where
  // it reads as instructions rather than an invitation.
  const inp = $("#input");
  if (!inp.dataset.placeholder) inp.dataset.placeholder = inp.placeholder;
  inp.placeholder = "问点什么…";
}

function clearFresh() {
  const c = $(".chat");
  if (!c.classList.contains("fresh")) return;
  c.classList.remove("fresh");
  const inp = $("#input");
  if (inp.dataset.placeholder) inp.placeholder = inp.dataset.placeholder;
  const hero = $(".fresh-hero");
  if (hero) hero.remove();
}

function addMsg(role, text) {
  clearFresh();
  const wrap = el("div", `msg ${role}`);
  const body = el("div", "bubble");
  if (text) body.textContent = text;
  wrap.appendChild(body);
  $("#messages").appendChild(wrap);
  scroll();
  return body;
}

function addUserMsg(text) {
  // Draw a prompt from the stream only if it is not already the last thing
  // said. Three independent painters put this same row on screen -- send()'s
  // optimistic echo, the stream's own `user` event, and the `history_user`
  // replayed when hermes reloads a session -- and the one-shot skip flag can
  // only cancel one of them, so whichever two happen to line up render the
  // prompt twice. A conversation cannot actually contain the same prompt twice
  // in a row with no reply between, so declining to draw it costs nothing and
  // covers every pairing at once. The pending row is skipped: it sits at the
  // tail while a reply is being waited for, and it is not something said.
  const rows = $("#messages").children;
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    if (row.id === "pending") continue;
    if (row.classList.contains("user") && row.textContent === text) return null;
    break;
  }
  return addMsg("user", text);
}

function dropLastUser() {
  // Undo the optimistic echo. `send()` draws the prompt before the server has
  // accepted it, so a refusal leaves a message on screen that the transcript
  // does not contain — and a reload would make it vanish, which reads as lost
  // rather than as rejected.
  const rows = $("#messages").querySelectorAll(".msg.user");
  const last = rows[rows.length - 1];
  if (last) last.remove();
}

/* ---------- the tail activity row ---------- */
// The header's spinner is at the TOP of a scrolling transcript, so during a long
// wait the reader is looking at the bottom where nothing moves -- reported as
// "I cannot tell if the model died or is thinking". This row lives at the TAIL,
// where the eye already is.
function showPending() {
  let row = $("#pending");
  if (!row) {
    row = el("div", "msg bot");
    row.id = "pending";
    const b = el("div", "bubble pending");
    b.append(el("span", "p-spin"), el("span", "p-text", "思考中"), el("span", "p-since"));
    b.querySelector(".p-since").dataset.since = String(Date.now());
    row.appendChild(b);
    $("#messages").appendChild(row);
    scroll();
  }
  return row;
}
function setPendingText(t) {
  const n = $("#pending .p-text");
  if (n) n.textContent = t;
}
function clearPending() {
  const row = $("#pending");
  if (row) row.remove();
}

/* ---------- segments ---------- */
function finalizeSeg() {
  if (!S.seg) return;
  const seg = S.seg;
  // Flush any render the rAF has not run yet, BEFORE dropping the reference it
  // needs. scheduleRender bails on `!S.seg`, so clearing first meant the last
  // batch of tokens was never painted: a live turn lost its closing sentences,
  // and a loaded session -- where every token arrives in one synchronous
  // forEach and the only render is the deferred one -- showed an empty bubble
  // where the whole answer should be.
  if (seg.kind === "out") {
    seg.body.innerHTML = renderMD(seg.text);
    renderMermaid(seg.body);   // async on purpose: the text is already on screen
  }
  S.seg = null;
  if (seg.kind === "think") { activitySummary(); return; }
  if (!seg.text.trim()) { seg.body.closest(".msg").remove(); return; }
  seg.body.classList.remove("streaming");
}

function activityGroup() {
  // One disclosure row per assistant turn, holding the thinking AND every tool.
  // Upstream's design guide is explicit about this: a turn that used ten tools
  // should read as one turn with one compact "Activity: 10 tools" row, not ten
  // chat cards. Ours rendered a card per tool and a separate row for thinking,
  // which is what buried the answer the reader came for.
  if (S.activity && document.body.contains(S.activity.wrap)) return S.activity;
  const wrap = el("div", "msg bot");
  const card = el("div", "bubble activity");
  const head = el("button", "act-head");
  const caret = el("span", "caret", "▸");
  const label = el("span", "act-label", "思考中");
  head.append(caret, label);
  const body = el("div", "act-body");
  const idx = S.actIndex++;
  const open = isOpen(idx);
  body.hidden = !open;
  caret.textContent = open ? "▾" : "▸";
  head.onclick = () => {
    body.hidden = !body.hidden;
    caret.textContent = body.hidden ? "▸" : "▾";
    setOpen(idx, !body.hidden);
  };
  card.append(head, body);
  wrap.appendChild(card);
  // The work goes ABOVE the answer, always — a fixed shape the reader can rely
  // on, not wherever the first tool event happened to land.
  //
  // This row used to be appended where it was created, which is faithful to
  // the event order and reads wrong whenever the answer comes first. Two ways
  // that happens, both seen in this store: a replayed assistant message that
  // carries BOTH text and tool_calls emits its text before its own calls
  // (hermes_session_api.history), and a live turn can answer and then keep
  // calling tools. Either way the reader got the conclusion, then the
  // reasoning under it.
  const top = S.turnTop && document.body.contains(S.turnTop) ? S.turnTop : null;
  if (top) $("#messages").insertBefore(wrap, top);
  else $("#messages").appendChild(wrap);
  S.activity = { wrap, card, head, caret, label, body, think: null, tools: 0 };
  scroll();
  return S.activity;
}

function activitySummary() {
  const a = S.activity;
  if (!a) return;
  const bits = [];
  if (a.tools) bits.push(`${a.tools} 个工具`);
  if (a.think && a.think.textContent.trim()) bits.push("思考");
  a.label.textContent = bits.length ? `活动 · ${bits.join(" · ")}` : "思考";
}

function newThinkSeg() {
  const a = activityGroup();
  if (!a.think) {
    a.think = el("div", "act-think");
    a.body.appendChild(a.think);
  }
  S.seg = { kind: "think", body: a.think, text: "", refs: null };
}

function newOutputSeg() {
  clearPending();      // tokens are their own proof of life
  const body = addMsg("bot", "");
  body.classList.add("streaming", "md");
  // The first answer bubble of this turn is where a later activity row slots
  // in above. Only the first: a turn that answers, tools again and answers
  // again still keeps ONE group, at the top of the whole turn.
  if (!S.turnTop) S.turnTop = body.closest(".msg");
  S.seg = { kind: "out", body, text: "", refs: null };
}

// Coalesce streamed tokens to one render per frame. Re-parsing the whole bubble
// on every token is what makes a long answer crawl.
let renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => {
    renderPending = false;
    if (!S.seg || S.seg.kind !== "out") return;
    S.seg.body.innerHTML = renderMD(S.seg.text);
    scroll();
  });
}

function appendThought(text) {
  if (!text) return;
  clearPending();
  status("思考中");
  if (!S.seg || S.seg.kind !== "think") { finalizeSeg(); newThinkSeg(); }
  S.seg.text += text;
  S.seg.body.textContent = S.seg.text;
  activitySummary();
  scroll();
}

function appendToken(text) {
  if (!text) return;
  status("生成回复");
  if (!S.seg || S.seg.kind !== "out") { finalizeSeg(); newOutputSeg(); }
  S.seg.text += text;
  scheduleRender();
}

/* ---------- tools ---------- */
function toolRow(id, title, st, detail, full, duration) {
  const a = activityGroup();
  let row = S.tools.get(id);
  if (!row) {
    row = el("div", "act-tool");
    row.append(
      el("span", "t-name", title || "tool"),
      el("span", "t-since"),
      el("span", "t-status", st || "")
    );
    a.body.appendChild(row);
    a.tools += 1;
    S.tools.set(id, row);
  }
  if (title) row.querySelector(".t-name").textContent = title;
  row.querySelector(".t-status").textContent = st || "";
  // Only a LIVE `running` starts the row's clock. It used to start on creation,
  // so a call replayed from the store — which is never running — ticked
  // forever when no result row came to stop it (production: a
  // mcp_memory_search_docs call read 40s, 43s… under a finished answer).
  const since = row.querySelector(".t-since");
  if (st === "running" && since && !since.dataset.since) since.dataset.since = String(Date.now());
  if (detail) {
    let d = row.nextElementSibling;
    if (!d || !d.classList.contains("act-detail")) {
      d = el("pre", "act-detail");
      row.after(d);
      // The row toggles only ITS detail. The group's caret is about the whole
      // activity; a tool's arguments and result are one level further down,
      // which is where the design guide puts them.
      row.classList.add("has-detail");
      row.onclick = () => { d.hidden = !d.hidden; d.dataset.touched = "1"; };
      d.hidden = true;
    }
    renderDetail(d, detail, full || detail.length);
    // A failure opens itself. The compact activity panel is right for ten
    // successful calls, but a reader who is told only "failed" has to guess
    // which row to click to find out why -- and the reason is right here.
    // Never override a reader who has already opened or closed it themselves.
    if (st === "failed" && d.dataset.touched !== "1") d.hidden = false;
  }
  row.dataset.status = st || "";
  if (st === "completed" || st === "failed") {
    const n = row.querySelector(".t-since");
    // Stop the client's own count and show the SERVER's measurement. Ours
    // starts when the row is painted, so it under-reports a tool whose start
    // event arrived late -- the row read 0s for a call the agent timed at 1.0s.
    if (n) { n.removeAttribute("data-since"); if (duration) n.textContent = duration; }
    // `owns`, not `busy`: a transcript replayed while another conversation's
    // turn kept the tab busy grew a 思考中 row it had nothing to do with.
    if (S.owns) { showPending(); setPendingText("处理检索结果"); }
  } else if (st === "running") { status(title || "工具执行中"); showPending(); }
  // Any other status (a replayed `pending`/`incomplete`, an unnamed progress
  // event) is a label on the row, not a claim that work is in flight.
  activitySummary();
  scroll();
}

// Clamp a tool result and let the reader open it, instead of cutting it and
// hoping the middle did not matter. `full` is the value's TRUE length: when the
// server hit its transport ceiling the tail never arrived, and saying so is the
// difference between "there is more, here it is" and a value that just stops.
const DETAIL_CLAMP = 1200;

function renderDetail(pre, text, full) {
  // A tool emits several `tool_call_update`s, and each one re-renders this
  // block. Read back the reader's own choice first: without it, a detail they
  // opened mid-run snapped shut the moment the tool reported progress.
  let expanded = pre.dataset.expanded === "1";
  pre.textContent = "";
  const short = text.length > DETAIL_CLAMP;
  const body = el("span", null, short ? text.slice(0, DETAIL_CLAMP) : text);
  pre.appendChild(body);
  if (!short && full <= text.length) return;

  const more = el("button", "more");
  const cut = full - text.length;          // never delivered
  const setLabel = (expanded) => {
    more.textContent = expanded
      ? "收起"
      : `显示全部（还有 ${full - DETAIL_CLAMP} 字${cut > 0 ? `，其中 ${cut} 字未传输` : ""}）`;
  };
  more.onclick = (e) => {
    e.stopPropagation();                    // the row's own toggle must not fire
    expanded = !expanded;
    pre.dataset.expanded = expanded ? "1" : "0";
    body.textContent = expanded ? text : text.slice(0, DETAIL_CLAMP);
    setLabel(expanded);
  };
  if (expanded) body.textContent = text;
  setLabel(expanded);
  pre.appendChild(more);
}

/* ---------- approvals ---------- */
function showApproval(a) {
  if (document.querySelector(`[data-perm="${a.id}"]`)) return;   // already on screen
  S.awaitingPerm = true;
  const wrap = el("div", "msg bot");
  const card = el("div", "bubble perm");
  card.dataset.perm = a.id;
  card.appendChild(el("div", "perm-title", a.title || "需要确认"));
  const opts = el("div", "perm-opts");
  (a.options || []).forEach((o) => {
    const b = el("button", "btn opt", o.name || o.optionId);
    b.onclick = () => answerApproval(a.id, o.optionId, card);
    opts.appendChild(b);
  });
  if (!(a.options || []).length) {
    const b = el("button", "btn opt", "取消");
    b.onclick = () => answerApproval(a.id, null, card);
    opts.appendChild(b);
  }
  card.appendChild(opts);
  wrap.appendChild(card);
  $("#messages").appendChild(wrap);
  status("等待你的确认");
  scroll();
}

async function answerApproval(id, optionId, card) {
  S.awaitingPerm = false;
  if (card) { card.classList.add("done"); card.querySelector(".perm-opts").remove(); }
  await fetch("/api/approval/answer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, optionId }),
  }).catch(() => {});
}

async function pollApprovals() {
  try {
    // Say which conversation we are in. Unscoped, this poll showed a second
    // person a permission prompt raised in a conversation they had never
    // opened — and let them answer it.
    // The `?` stays OUTSIDE the interpolation: a reader — and the test that
    // checks the client only asks for paths the router serves — cannot tell a
    // query from a path segment when the separator is hidden inside `${…}`.
    const q = S.sessionId ? `session=${encodeURIComponent(S.sessionId)}` : "";
    const j = await (await fetch(`/api/approval/pending?${q}`)).json();
    const p = (j.pending || [])[0];
    if (p) showApproval(p);
    else S.awaitingPerm = false;
  } catch (_) { /* transient; the next tick retries */ }
}

/* ---------- the stream ---------- */
function apply(ev, from) {
  // Does this belong to what the reader is looking at? Browsing mid-turn means
  // the answer can be no, and a stream that paints regardless puts one
  // conversation's tokens into another's transcript. The turn is untouched;
  // only the drawing is skipped. `end` still runs, because the turn really did
  // end and the bookkeeping it does is not about the screen.
  //
  // A conversation that does not exist yet cannot be identified by session id —
  // it has none until hermes assigns one — so for that view the question is
  // "did MY send start this stream", not "does this id match". Asking the id
  // there matched everything, because null matches nothing and the check let it
  // through: another session's reply drew straight into the new one.
  //
  // paintHistory() replays a committed transcript through this same router:
  // those events carry no session and arrive with no source stream. The store
  // is the authority on what a conversation said — trust them outright.
  // (Live events always carry `session` and come with `from` set; the gate
  // below exists to route THOSE.)
  const replay = from === undefined;
  // openSession is reading this conversation's history, and a live turn will
  // then be replayed from seq 0 — a frame drawn now would be drawn twice. `end`
  // still runs: it closes the feed, and the replay delivers it again.
  if (!replay && S.switching && ev.session === S.switching && ev.kind !== "end") return;
  const fresh = S.pendingNew && !S.sessionId;
  // hermes ROTATES the session id under a long conversation (context
  // compression continues under a new id). Frames after the rotation carry the
  // new id and would fail the ownership test forever — the view starves
  // mid-answer. On the FOCUS stream the frame's id is authoritative: adopt it
  // BEFORE the test, or the rotation frame itself is dropped as foreign.
  if (!fresh && ev.session && S.sessionId && ev.session !== S.sessionId
      && from === S.streamId) {
    S.sessionId = ev.session;
    rememberView(ev.session);
    noteSessionEndpoint(ev.session);
    loadSessions();
  }
  const mine = replay
    ? true
    : fresh
      ? (!!S.ownStream && from === S.ownStream)
      // Every frame carries its session (TurnStream.emit injects it), so ids
      // decide — EXCEPT the reload-recovery view, whose S.sessionId is still
      // null: there the FOCUS stream is the recovered turn and is trusted, and
      // a session-LESS frame likewise only on the focus stream. With several
      // feeds open, another turn's frames must not paint here.
      : (!S.sessionId || !ev.session)
        ? from === S.streamId
        : ev.session === S.sessionId;
  // The stream is the authority on which conversation this turn belongs to.
  // Reading `acp.session_id` instead raced hermes assigning it and could hand
  // back the PREVIOUS session, relabelling the new conversation as the old one.
  if (mine && fresh && ev.session) {
    S.sessionId = ev.session;
    rememberView(ev.session);
    noteSessionEndpoint(ev.session);   // the pending choice belongs to this id now
    S.pendingNew = false;
    loadSessions();
  }
  if (!mine && ev.kind !== "end") {
    // Advance the cursor only for the FOCUS stream: a background turn's seq
    // is not a position in the turn on screen.
    if (ev.seq && from === S.streamId) S.lastSeq = ev.seq;
    return;
  }
  switch (ev.kind) {
    case "user":
      // We already drew this optimistically in send(), so the stream's echo of
      // the SAME message would render it twice. A reattach after reload draws
      // nothing first, so there the echo is exactly what paints it.
      finalizeSeg();
      // A new prompt closes the previous turn's activity group. Without this,
      // a REPLAYED multi-turn transcript funnels every later turn's tools into
      // the FIRST turn's group -- that group sits after the first question, so
      // the last question on screen looked like it had run nothing, while a
      // wall of unrelated tool rows stacked up above it. (Live turns never hit
      // this: endTurn already nulled S.activity before the next prompt.)
      S.activity = null; S.turnTop = null;
      S.tools.clear();
      if (S.skipUserEcho) S.skipUserEcho = false;
      else addUserMsg(ev.text);
      break;
    case "history_user":
      finalizeSeg();
      // Same closure as above: each replayed prompt starts a fresh activity
      // group, so the tools that follow it render AFTER that prompt.
      S.activity = null; S.turnTop = null;
      S.tools.clear();
      addUserMsg(ev.text);
      break;
    case "delta": ev.thought ? appendThought(ev.text) : appendToken(ev.text); break;
    case "tool": toolRow(ev.id, ev.title, ev.status, ev.detail, ev.detailFull, ev.duration); break;
    case "approval": showApproval(ev); break;
    case "approval_expired":
      S.awaitingPerm = false;
      finalizeSeg(); addMsg("note", "审批超时，本次操作已取消");
      break;
    case "note": finalizeSeg(); addMsg("note", ev.text); break;
    // A turn that died, replayed with the transcript. The live path reports a
    // failure through `end`, which only reaches whoever was watching; this is
    // the same failure for the reader who reloaded or was elsewhere, and
    // without it the conversation reads as a question the app silently
    // dropped rather than one the engine refused.
    case "error": finalizeSeg(); addMsg("error", `⚠ ${ev.text}`); break;
    case "gap": finalizeSeg(); addMsg("note", "（断线期间有部分输出未能保留）"); break;
    case "end": endTurn(ev.error, ev.session, from); break;
  }
  // Per-stream: a BACKGROUND feed's frames must not advance the focus cursor.
  if (ev.seq && from === S.streamId) S.lastSeq = ev.seq;
}

function attach(streamId, afterSeq, opts) {
  // One EventSource PER TURN, held in `S.ess`, not one slot per view. Two
  // endpoints mean two turns can run at once, and closing the previous feed
  // on every attach was how watching a second session froze the first: the
  // orphaned turn kept running server-side (streams buffer regardless of
  // readers) while its view starved — and switching back re-orphaned the
  // other one. A seesaw; both ends look dead.
  S.streamId = streamId;   // where 停止 and reload-recovery aim
  S.lastSeq = afterSeq || 0;
  setBusy(true);
  // `replay` (openSession): the view was just cleared and repainted from the
  // store, so the live turn must be re-delivered from the top, not resumed
  // from a cursor pointing into a feed this DOM no longer contains.
  if (opts && opts.replay) closeEs(streamId);
  if (S.ess.has(streamId)) return;   // already subscribed; its cursor is live
  const es = new EventSource(
    `/api/chat/stream?stream_id=${encodeURIComponent(streamId)}&after_seq=${S.lastSeq}`
  );
  S.ess.set(streamId, es);
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (_) { return; }
    apply(ev, streamId);
  };
  es.onerror = async () => {
    // EventSource retries forever on its own, so "reconnecting" can outlive the
    // thing it claims to be reconnecting to: a server restart drops every live
    // stream AND the in-process turn behind it, and the banner then sits there
    // permanently while nothing is coming. Ask before claiming. A BACKGROUND
    // feed (not the one on screen) fails silently: close it, keep the screen
    // honest, let the next openSession resubscribe if the turn is still there.
    if (streamId !== S.streamId) { closeEs(streamId); return; }
    if (!S.busy) return;
    status("连接中断,重连中…");
    try {
      const st = await (await fetch(
        `/api/chat/status?stream_id=${encodeURIComponent(streamId)}`
      )).json();
      if (!st.known) {
        // The turn is gone with the process that held it. Say so plainly rather
        // than pretending a reconnect is in progress.
        closeEs(streamId);
        finalizeSeg();
        setBusy(false);
        addMsg("note", "服务重启，这一轮的回复已丢失");
        status("就绪");
      } else if (!st.running) {
        // It finished while we were disconnected; the reconnect will collect
        // the tail, so leave the stream alone and stop alarming the reader.
        status("接回中…");
      }
    } catch (_) { /* the server is genuinely unreachable — keep retrying */ }
  };
}

function closeEs(streamId) {
  const es = S.ess.get(streamId);
  if (es) { es.close(); S.ess.delete(streamId); }
}


function endTurn(error, owner, from) {
  // One turn ended. Only ITS feed closes — other live turns keep streaming;
  // only the SCREEN's mutations happen — a foreign end must not unfreeze this
  // view's composer or clear its drawing.
  if (from) closeEs(from);
  // A view with no id is either a pending 新会话 — only ITS own send's end is
  // shown, or another conversation's error lands in the empty view — or a
  // reload recovery, which shows its focus stream.
  const shown = S.sessionId && owner
    ? owner === S.sessionId
    : S.pendingNew && !S.sessionId
      ? (!!from && from === S.ownStream)
      : (!from || from === S.streamId);
  if (owner) HISTORY_CACHE.delete(owner);
  if (S.sessionId) HISTORY_CACHE.delete(S.sessionId);
  if (shown) {
    clearPending();         // only ours: a foreign end took this view's 思考中 row
    finalizeSeg();
    S.activity = null; S.turnTop = null;   // the next turn opens its own group
    setBusy(false);
    S.tools.clear();
    S.awaitingPerm = false;
    if (error) addMsg("error", `⚠ ${error}`);
  }
  if (shown) {
    const wasStopping = S.stopping;
    S.stopping = false;
    status(error ? "出错" : wasStopping ? "已停止" : "就绪");
  }
  loadSessions();   // the turn may have created or retitled a session
}

function cancelTurn() {
  // A REQUEST, not an instant stop: the agent finishes the chunk it is on
  // first, measured at ~12 s here. Without saying so the button reads as
  // broken, which is exactly how it was reported.
  if (!S.owns || S.stopping) return;        // a second click adds nothing; and
                                            // never a turn this view does not show
  S.stopping = true;
  $("#send").textContent = "停止中…";
  $("#send").disabled = true;
  status("正在停止,等 agent 收尾…");
  const rearm = (msg) => {
    S.stopping = false;
    $("#send").textContent = "停止";
    $("#send").disabled = false;
    status(msg);
  };
  // Name the target. The server refuses to guess between two live turns, and it
  // should: stopping the wrong conversation is worse than not stopping.
  fetch("/api/chat/cancel", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId: S.sessionId || "", streamId: S.streamId || "" }),
  }).then(async (r) => {
    const j = await r.json().catch(() => ({}));
    // 409: the agent is wedged in a tool AND another session is replying, so
    // the server declined to restart the process out from under it. Say that,
    // rather than leaving a button stuck on "停止中…" with no explanation.
    if (j && j.how === "unyielding") rearm("agent 未响应停止，且另有会话在回复，暂无法强制中断");
  }).catch(() => rearm("停止请求发送失败"));
}

function setBusy(b) {
  S.busy = b;
  if (!b) S.stopping = false;
  const send = $("#send");
  // Whose turn is this? If the view has an id, the ids decide — full stop.
  // `S.ownStream` is "the stream THIS CLIENT started", not "the stream of the
  // session on screen", so consulting it after navigating away kept claiming a
  // turn left behind in another conversation. It is meaningful only while the
  // view has no id of its own yet, which is the one window ids cannot cover.
  // With no id and no pending 新会话, the view is a reload recovery: the focus
  // stream IS the view's turn.
  const owns = b && (S.sessionId
    ? !!S.streaming[S.sessionId]
    : S.pendingNew
      ? (!!S.ownStream && S.ownStream === S.streamId)
      : !!S.streamId);
  // Stored, because the 停止 button, Enter and the form all have to ask THIS
  // question. They used to ask `S.busy` — true whenever the tab's focus stream
  // is live, including a turn left running in another conversation — so 发送
  // in a new session ran cancelTurn() against that other turn and its reply
  // was lost.
  S.owns = !!owns;

  // Ask the server how many replies the CHOSEN endpoint can run and how many
  // it is running, instead of reading "a turn exists" as "the composer is
  // blocked". The limit is a fact about the MODEL behind the picker's choice —
  // one endpoint being full must not grey out a send aimed at another.
  // A turn is running that this view does not own. Computed here and stored,
  // because the status line used to decide the same thing for itself and the
  // two disagreed: the line said another session was replying while the button
  // stayed clickable. The button's capacity test fell back to THIS view's busy
  // flag, which is false precisely when the turn belongs to someone else.
  const othersLive = Object.keys(S.streaming).some((id) => id !== S.sessionId);
  const chosen = S.endpoints.find((e) => e.key === S.endpoint) || null;
  const atCapacity = chosen && chosen.maxConcurrent != null
    ? (chosen.running || 0) >= chosen.maxConcurrent
    // Before the first list refresh the counters are unknown. Fall back CLOSED:
    // a composer that accepts a message the pool has no room to run is worse
    // than one that makes the reader wait a moment for the real numbers.
    : (b || othersLive);
  const elsewhere = atCapacity && !owns;
  // The one fact the rest of the UI reads, so nothing recomputes it.
  S.blockedElsewhere = !!elsewhere;

  send.textContent = owns ? "停止" : "发送";
  send.classList.toggle("stop", !!owns);
  send.disabled = !!elsewhere;
  send.title = elsewhere
    ? (chosen
      // Same words the server refuses with, so the 429 backstop and this
      // button tell one story.
      ? `${chosen.label} 已有 ${chosen.running || 0} 个会话在回复，达到上限 ${chosen.maxConcurrent} — 等它结束，或换一个端点`
      : "另一个会话正在回复 — 等它结束，或到那个会话里停止它")
    : "";
  $(".chat").classList.toggle("blocked", !!elsewhere);
  // Say it in the placeholder rather than in a line under the box. A tooltip
  // needs a hover and the reason a button will not respond should not; a line
  // under the composer needs neither, but it belongs to nothing on screen and
  // moves the layout when it appears. The input stays typable — waiting is a
  // fine time to write the next message — so the placeholder is showing
  // exactly when the reader has nothing else to read.
  const input = $("#input");
  if (input.dataset.idlePlaceholder === undefined) {
    input.dataset.idlePlaceholder = input.placeholder;
  }
  input.placeholder = elsewhere
    ? (chosen
      ? `${chosen.label} 已满 · 一次只跑 ${chosen.maxConcurrent} 轮`
      : "另一个会话正在回复")
    : input.dataset.idlePlaceholder;
  if (b) {
    S.startedAt = Date.now();
    if (!S.timer) S.timer = setInterval(() => { tick(); tickSlow(); }, 90);
    tick(); tickSlow();
    // Polling backstop for approvals: the push rides the same stream a proxy or
    // a sleeping laptop can drop, and a missed prompt stalls the turn with
    // nothing on screen to explain it.
    if (!S.approvalTimer) S.approvalTimer = setInterval(pollApprovals, 1500);
  } else {
    S.startedAt = 0;
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
    if (S.approvalTimer) { clearInterval(S.approvalTimer); S.approvalTimer = null; }
    tick();
  }
}

/* ---------- sessions ---------- */
function renderSessions() {
  // Pure render from the rows we already have. Split out of `loadSessions` so a
  // click can repaint the sidebar with no network in it at all: the fetch it
  // used to wait on costs ~310 ms, and the highlight moving only after that —
  // plus another ~310 ms for the transcript — is what made switching feel slow.
  // The reference webui does the same thing for the same reason
  // (`renderSessionListFromCache`).
  const ul = $("#sessions");
  // A project page lists ITS conversations only: each session belongs to the
  // project it was opened under (recorded at chat/start), and a page that
  // names /buda/ in its URL shows buda's. Sessions from before profiles —
  // no recorded key — file under the default profile, which is what they
  // served as. An EMPTY profile (the page opened at /) filters nothing —
  // there is no project to be scoped to, so the list is everything.
  const rows = S.profile
    ? (S.sessionRows || []).filter((s) => (s.profile || S.defaultProfile) === S.profile)
    : (S.sessionRows || []);
  ul.textContent = "";
  $("#sess-count").textContent = rows.length ? String(rows.length) : "";
  rows.forEach((s) => {
    let cls = s.id === S.sessionId ? "sess cur" : "sess";
    if (s.is_streaming) cls += " streaming";
    const li = el("li", cls);
    // A compression chain renders N rows with the SAME title (every segment
    // opens with the same question), so `preview` duplicates `title` and the
    // rows are indistinguishable — which is how two segments got deleted as
    // "duplicates". When the second line would say nothing new, say WHEN and
    // HOW MUCH instead: that is what tells one segment from another.
    const sub = (s.preview && s.preview !== s.title)
      ? s.preview
      : [fmtWhen(s.lastActive), s.messageCount != null ? `${s.messageCount} 条` : ""]
          .filter(Boolean).join(" · ") || "";
    li.append(el("div", "t", s.title || "(未命名)"), el("div", "p", sub));
    li.onclick = () => openSession(s.id);
    const del = el("button", "del", "×");
    del.title = "删除会话";
    del.onclick = (e) => { e.stopPropagation(); removeSession(s); };
    li.appendChild(del);
    ul.appendChild(li);
  });
}

function fmtWhen(ts) {
  // Sidebar-sized timestamp: today shows the clock, earlier shows the date
  // too. Server timestamps are seconds.
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
}

function watchWhileOthersRun() {
  // The page follows ONE stream at a time — the conversation on screen. A turn
  // running anywhere else has no connection to this tab at all, so nothing
  // would ever tell the sidebar it finished. This is that poll — fast while
  // something streams, and SLOW forever otherwise: sessions deleted elsewhere
  // (another tab, an operator) used to leave their rows standing until some
  // unrelated action happened to re-fetch. One list query per 15 s is nothing.
  const anyLive = Object.keys(S.streaming).length > 0;
  const want = anyLive ? 3000 : 15000;
  if (S.watchPeriod !== want) {
    if (S.watchTimer) clearInterval(S.watchTimer);
    S.watchTimer = setInterval(loadSessions, want);
    S.watchPeriod = want;
  }
}

async function loadSessions() {
  let j;
  try { j = await (await fetch("/api/sessions")).json(); } catch (_) { return; }
  // `j.current` is deliberately NOT adopted. Setting S.sessionId here moved the
  // sidebar highlight without painting that transcript, and aimed the next
  // send at a conversation the reader could not see. Only openSession and a
  // send decide what is on screen; boot() decides what a reload reopens.
  const was = S.streaming;
  S.streaming = j.streaming || {};
  // A turn can now finish in a conversation this page is not watching, and its
  // committed transcript only exists once it has. Drop the cached history of
  // anything that stopped streaming, or a switch back would repaint the stale
  // copy taken before the reply landed.
  Object.keys(was).forEach((id) => { if (!S.streaming[id]) HISTORY_CACHE.delete(id); });
  watchWhileOthersRun();
  S.sessionRows = j.sessions || [];
  // The default profile rides every sessions poll, so the sidebar's filter
  // is correct even before /api/status answers (a reload races them).
  if (j.defaultProfile) S.defaultProfile = j.defaultProfile;
  if (j.endpoints) setEndpoints(j.endpoints, null);
  setBusy(S.busy);          // capacity changed under it; re-render the composer
  renderSessions();
}


// Transcripts already replayed in this page's lifetime. A switch back is then
// a repaint, not a round trip — which matters because `session/load` costs
// about a second: hermes re-registers the MCP server and rebuilds its whole
// tool surface on EVERY load, regardless of how short the transcript is.
const HISTORY_CACHE = new Map();

function paintHistory(history) {
  // Skip empty replayed messages. A turn that produced only a tool call, or was
  // cancelled before its first token, leaves a text-less entry that renders as
  // a blank bubble the reader cannot account for.
  history
    .filter((ev) => ev.kind !== "delta" || (ev.text || "").length)
    // NOT `.forEach(apply)`: forEach passes the INDEX as the second argument,
    // so `from` arrived as 0, 1, 2… instead of undefined, the replay test in
    // apply() never matched, and every history event was dropped as foreign —
    // opening any conversation drew an empty panel.
    .forEach((ev) => apply(ev));
  finalizeSeg();
}

async function openSession(id) {
  // Every await below re-checks this. A second click on the row already
  // loading used to paint the transcript twice: both calls passed a "still
  // this session id" check. The first call's generation is stale, so it stops.
  const gen = ++S.viewGen;
  // An approval card belongs to the conversation being left, and its card is
  // about to be erased. Left raised, every send in the tab answered
  // "先回答上方的确认请求" with nothing on screen to answer — and nothing lowers
  // it, because the approval poll stops with the busy state. Returning to that
  // conversation replays the approval and raises it again.
  S.awaitingPerm = false;
  clearFresh();
  S.ownStream = null;       // whatever we started, we are not looking at it now
  // Leaving a pending 新会话 for a real conversation. Left raised, the next
  // send went out as `new: true` and opened ANOTHER conversation instead of
  // continuing the one on screen.
  S.pendingNew = false;
  // The focus stream belongs to the conversation being LEFT. Kept, the rotation
  // rule in apply() read that turn's next token as "the session on screen was
  // renamed" and pulled the view back to it, drawing its reply under this
  // transcript. attach() below sets it again if THIS session is live.
  S.streamId = null;
  S.sessionId = id;
  rememberView(id);
  // The picker follows the conversation: show the model THIS session uses.
  // A session from another tab has no recorded choice — it falls to the
  // last-used default; a stale key (endpoint gone) falls to the first.
  const want = endpointFor(id);
  S.endpoint = S.endpoints.find((e) => e.key === want) ? want
    : (S.endpoints[0] ? S.endpoints[0].key : null);
  renderEndpoints();
  setBusy(S.busy);
  renderSessions();         // the selection moves NOW, not after two round trips
  // No busy guard. Reading a transcript no longer moves the agent, so a turn
  // in flight is none of this function's business — it keeps streaming into
  // whichever session owns it, and `send()` relocates the agent when the reader
  // actually says something. Telling someone to "wait or stop the current
  // reply" just to LOOK at another conversation was the whole complaint.
  $("#messages").textContent = "";
  S.tools.clear(); S.seg = null; S.activity = null; S.turnTop = null; S.actIndex = 0; S.sessionId = id;

  // Paint what we already have BEFORE asking the server, then refresh from the
  // store. The request moves nothing — sessions are addressed by id now, so a
  // send lands where it is told regardless of what was read — it is only how
  // the transcript stays honest when the cached copy predates the last turn.
  const cached = HISTORY_CACHE.get(id);
  if (cached) paintHistory(cached);
  S.switching = id;
  status(cached ? "切换中…" : "载入中…");

  // `S.streaming` is up to one sidebar poll old. A turn that ended since then
  // is committed to the store, so replaying its stream as well painted that
  // turn twice. Ask — BEFORE the history read: a turn ending between the two
  // then costs a duplicate, never a missing turn, which the other order would.
  let liveStream = S.streaming[id] || null;
  if (liveStream) {
    try {
      const st = await (await fetch(`/api/chat/status?stream_id=${encodeURIComponent(liveStream)}`)).json();
      if (!st.running) { delete S.streaming[id]; liveStream = null; }
    } catch (_) { /* unreachable: trust the map, the replay reports what it finds */ }
    if (gen !== S.viewGen) return;
  }

  let j;
  try {
    // Query string, not a path segment. The server's router is an exact
  // (method, path) dict with no parameter support, so an id interpolated into
  // the path matches no route and 404s.
  j = await (await fetch(`/api/session/history?id=${encodeURIComponent(id)}`)).json();
  } catch (_) {
    if (gen === S.viewGen) { S.switching = null; status("载入会话失败"); }
    return;
  }
  // A switch the reader started and then abandoned: they are looking at another
  // view now, so painting this one's history would corrupt what they see. The
  // generation, not the id: 打开 A → 新会话 → 打开 A leaves the id equal while
  // the first call's view is long gone.
  if (gen !== S.viewGen) return;
  S.switching = null;
  if (j.error) { status(`载入失败: ${j.error}`); return; }
  // An EMPTY transcript for a session the sidebar says has messages means the
  // row is gone (deleted in another tab) or never persisted. Say so — a silent
  // empty panel read as "the messages are lost". Not for a LIVE session: a
  // first turn persists only when it ends, so its store is empty while it
  // runs — returning here left a reloaded page never attaching to the reply.
  if (!(j.events || []).length && !liveStream) {
    const row = (S.sessionRows || []).find((r) => r.id === id);
    finalizeSeg();
    addMsg("note", (row && row.messageCount) ? "这个会话的内容已不可读（可能已在别处删除）" : "这个会话还没有内容");
    status("就绪");
    return;
  }
  // `events`, which is what `/api/session/history` returns. Reading `history`
  // here yielded undefined and painted an empty transcript — invisible until
  // now only because the old URL 404'd before reaching this line.
  const events = j.events || [];
  HISTORY_CACHE.set(id, events);
  // Paint the fetched events whenever the cached copy is empty — a session
  // clicked once WHILE its first turn was running got cached as [], and the
  // old `if (!cached)` guard turned that into an empty panel forever.
  if (!cached || !cached.length) paintHistory(events);
  // Not gated on S.busy any more: with several turns possible, the one that
  // matters is whether THIS conversation is streaming — which the map answers
  // directly. The old check asked "is anything streaming, and is it this one",
  // and its first half is no longer a useful question.
  if (liveStream) {
    // Back on a conversation that is running. The store stops where the
    // committed transcript does, so replay the live turn from the top rather
    // than showing a reply that appears to have stopped mid-sentence.
    // `replay` re-opens THIS turn's feed even if one was already open — the
    // repaint above erased what it had drawn — while every OTHER live feed
    // stays connected, which is the whole point of the per-stream map.
    S.skipUserEcho = false;
    attach(liveStream, 0, { replay: true });
    status("回复中…");
  } else {
    // No global detach: other sessions' feeds stay subscribed in the
    // background, filtered out by `apply` until their turn is on screen.
    setBusy(false);
    // setBusy just decided this; asking it again here is how the line and the
    // button drifted apart.
    status(S.blockedElsewhere ? "另一个会话仍在回复中" : "就绪");
  }
  loadSessions();
}

async function removeSession(s) {
  // Ask first — and name the row SPECIFICALLY. The chain segments share one
  // title, so a confirm that says only 删除会话「什么是怨憎会苦」 deletes a
  // different conversation than the one the reader thinks they named. Time
  // and size are what actually identify the row.
  const id = s.id;
  const name = (s.title || "").trim() || id.slice(0, 8);
  const meta = [fmtWhen(s.lastActive), s.messageCount != null ? `${s.messageCount} 条消息` : ""]
    .filter(Boolean).join(" · ");
  if (!confirm(`删除会话「${name}」${meta ? `（${meta}）` : ""}?此操作不可撤销。`)) return;
  // POST to a real route, and SHOW a failure. The old call was
  // `DELETE /api/session/<id>` — a path the exact-match router cannot serve —
  // with `.catch(() => {})` on the end, so every delete 404'd silently and the
  // row reappeared with no explanation.
  try {
    const r = await fetch("/api/session/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: id }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { status(`删除失败：${j.error || `HTTP ${r.status}`}`); return; }
  } catch (e) {
    status(`删除失败：${e.message}`);
    return;
  }
  // The conversation on screen is gone: say so with the fresh view rather than
  // a transcript of something that no longer exists.
  if (S.sessionId === id) newSession();
  else loadSessions();
}

async function newSession() {
  // Purely local, and deliberately creates nothing. A session made on a click
  // is a session most readers never write into, and the store filled up with
  // titleless zero-message rows exactly that way. The intent is recorded and
  // the session is created by the send that gives it something to hold.
  S.pendingNew = true;
  S.viewGen++;
  S.switching = null;       // a history read still in flight is for a view now gone
  S.awaitingPerm = false;   // see openSession: the card leaves with the view
  S.ownStream = null;       // nothing on screen is ours until we send
  // The turn in flight keeps running and keeps its stream; only the drawing
  // stops, because `apply` now checks who each event belongs to. Detaching or
  // calling `endTurn` here would abandon a live reply.
  $("#messages").textContent = "";
  S.seg = null; S.tools.clear(); S.activity = null; S.turnTop = null; S.actIndex = 0; S.sessionId = null;
  S.streamId = null;        // the left conversation's turn is no longer the focus
  rememberView(null);      // a reload now opens on 新的对话, as the screen does
  showFresh();
  setBusy(false);    // the new view owns nothing — same as openSession on an idle
                     // conversation. The other turn's feed stays in S.ess.
  status(S.blockedElsewhere ? "另一个会话仍在回复中" : "就绪");
  loadSessions();
}

/* ---------- sending ---------- */
async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text) return;
  if (S.awaitingPerm) {
    // Never a silent return: you type, press Enter, nothing happens, and the
    // reason (an approval is blocking the agent) is invisible. Point at it.
    const card = document.querySelector("[data-perm]");
    if (card) card.scrollIntoView({ block: "nearest" });
    status("⚠ 先回答上方的确认请求");
    return;
  }
  if (S.owns) return;   // this conversation is already replying
  if (S.switching) { status("正在切换会话，稍候"); return; }
  input.value = ""; input.style.height = "auto";
  addMsg("user", text);
  S.seg = null;
  S.activity = null; S.turnTop = null;
  S.skipUserEcho = true;
  const gen = S.viewGen;    // the view this send was typed into
  let j;
  try {
    j = await (await fetch("/api/chat/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        // Which conversation this belongs to. The server addresses it by id
        // and introduces it to the ACP process if that process has not seen it
        // yet; nothing is "moved", so a turn running elsewhere is unaffected.
        sessionId: S.pendingNew ? "" : (S.sessionId || ""),
        new: !!S.pendingNew,
        // Which endpoint answers. Unset means the server's default; the choice
        // survives reloads in localStorage. A conversation may switch endpoints
        // between turns — the agent cache keys on (model, base_url, provider),
        // so the switch is a rebuild against the new one, history intact.
        endpoint: S.endpoint || undefined,
        // Which project this page serves — read off the URL (/buda/ → buda),
        // not chosen by a widget. The server echoes the resolved key; a stale
        // bookmark that outlived a rename falls back server-side, and the
        // echo below is adopted only to keep the title honest.
        profile: S.profile || undefined,
      }),
    })).json();
  } catch (_) { status("发送失败"); return; }
  if (gen !== S.viewGen) {
    // The reader moved — clicked a row or 新会话 — while the request was out.
    // Adopting the response now would stamp this conversation onto THAT view:
    // its id, its saved position, and its reply drawn under someone else's
    // transcript. The turn itself started and keeps running; record it so the
    // sidebar marks it live and opening it replays the reply from the top.
    S.skipUserEcho = false;
    if (j.error) {
      if ((j.busy || j.taken) && !input.value) input.value = text;   // never eat what was typed
      status(j.error);
    } else if (j.sessionId && j.streamId) {
      S.streaming[j.sessionId] = j.streamId;
      if (j.endpoint) { S.sessionEp[j.sessionId] = j.endpoint; saveSessionEp(); }
    }
    loadSessions();
    return;
  }
  if (j.error) {
    // Two ways a send can be refused, and neither may eat what was typed:
    // `busy` is the server at capacity (the composer should already have been
    // blocked, so this is the race backstop), `taken` is someone else already
    // replying in this conversation.
    if (j.busy || j.taken) { input.value = text; dropLastUser(); }
    status(j.error);
    if (j.taken) loadSessions();   // the sidebar did not know it was streaming
    return;
  }
  if (!j.streamId) { status("没有可用的会话流"); return; }
  S.pendingNew = false;
  // The server echoes the endpoint it actually used. A saved key the server
  // no longer advertises falls back to ITS default silently — adopting the
  // echo here keeps the picker honest about what will answer next time.
  if (j.endpoint && j.endpoint !== S.endpoint) {
    S.endpoint = j.endpoint;
    saveEndpoint(j.endpoint);
    renderEndpoints();
    setBusy(S.busy);
  }
  noteSessionEndpoint(S.sessionId);   // pre-existing session: pin what it uses
  // `attached` means a turn was ALREADY running and we joined it -- its prompt
  // is not the one we just drew, so let the echo paint it.
  if (j.attached) S.skipUserEcho = false;
  // A brand-new conversation gets its id from hermes only now, so this is the
  // first moment the sidebar can show it. Refreshing here rather than at the
  // end of the turn is the difference between "it appears when you ask" and
  // "it appears when the answer finishes".
  // Ours, so `apply` can tell our own turn from one still running elsewhere
  // while this view has no id of its own yet.
  S.ownStream = j.streamId;
  if (j.sessionId) {
    S.sessionId = j.sessionId; noteSessionEndpoint(j.sessionId); S.pendingNew = false;
    rememberView(j.sessionId);
    // Known now, not at the next /api/sessions: `owns` reads this map, and
    // until it says so the view would offer 发送 on its own running turn.
    S.streaming[j.sessionId] = j.streamId;
  }
  loadSessions();
  showPending();
  attach(j.streamId, 0);
}

/* ---------- boot ---------- */
async function boot() {
  loadSessions();
  fetch("/api/status").then((r) => r.json()).then((j) => {
    // The profile this page serves comes off the URL (/buda/ → buda). The
    // server's list is the truth: an unknown key (renamed profile, stale
    // bookmark) sends the reader back to the home page's cards, the same
    // stale-key fallback the API applies — navigation instead of silence.
    const keys = new Set((j.profiles || []).map((p) => p.key));
    if (!keys.has(S.profile)) { location.replace("/"); return; }
    S.defaultProfile = j.defaultProfile || (j.profiles || [])[0]?.key || null;
    // The header says WHICH project this is — the page's own name, from the
    // same list that validated the URL.
    const p = (j.profiles || []).find((x) => x.key === S.profile);
    const name = $("#proj-name");
    if (name && p) name.textContent = p.label || p.key;
    document.title = `${p ? p.label || p.key : S.profile} · deepwiki`;
    // The list of endpoints and the default are the server's to declare.
    setEndpoints(j.endpoints || [], j.defaultEndpoint || null);
  }).catch(() => {
    $("#model-badge").textContent = "模型未配置";
    $("#model-badge").hidden = false;
  });
  // The change handler, not an inline call: it re-renders, persists, and
  // recomputes what the composer is allowed to do under the new choice.
  $("#endpoint").addEventListener("change", (e) => pickEndpoint(e.target.value));

  // Reopen what was on screen BEFORE accepting input — through openSession, the
  // same path a sidebar click takes: the transcript comes from the store, and a
  // turn still running is replayed from its first event, so a reply that
  // survived the reload comes up visibly running rather than looking idle.
  // The session list comes first: it is what says whether the view still
  // exists and which stream, if any, is live in it.
  try {   // the retired stream cursor; left behind, it is only noise
    localStorage.removeItem("hermes.streamId"); localStorage.removeItem("hermes.lastSeq");
  } catch (_) {}
  const view = recallView();
  const gen = S.viewGen;
  await loadSessions();
  if (gen !== S.viewGen) {
    // The reader already chose — the sidebar rendered during the await and
    // they clicked a row or 新会话. Reopening the saved view would override it.
  } else if (view && belongsHere(view)) {
    // `belongsHere` covers a LIVE turn too: /api/sessions synthesizes a row
    // for a conversation whose first turn has not persisted yet, carrying the
    // project it was started under — so a running turn in another project no
    // longer drags its transcript onto this page either.
    openSession(view);
  } else {
    // Nothing to reopen: this IS a fresh conversation, so say so rather than
    // opening on a blank panel that reads as loading — and MEAN it, so the
    // first message starts a new conversation.
    S.pendingNew = true;
    showFresh();
  }

  // The architecture note keeps its modal markup and close handling — but no
  // header button opens it any more. It stays reachable only by keeping the
  // markup, in case a future entry point wants it back.
  $("#about-close").onclick = () => { $("#about").hidden = true; };
  $("#about").onclick = (e) => { if (e.target.id === "about") $("#about").hidden = true; };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#about").hidden) $("#about").hidden = true;
  });

  // 新会话 is a per-project action: it creates a fresh conversation INSIDE
  // the project this page serves (the send carries S.profile, so the pin is
  // right). ONE control, in the header — the sidebar used to carry a second
  // ＋ 新会话 at the tail of the session list, two inches from this one and
  // doing exactly the same thing. The key still works (Cmd/Ctrl+K).
  const nsBtn = $("#new-session");
  if (nsBtn) nsBtn.onclick = () => { newSession(); };
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      newSession();
    }
  });

  // Theme toggle — deepwiki's icon button. Dark is the original look and the
  // default; light is the same layout with a daylight palette (style.css
  // [data-theme="light"]). Persisted per browser; applied before first paint
  // via the inline script in <head> so a light reader never sees a dark flash.
  //
  // WHICH icon is up is CSS's business (both sun and moon are in the markup,
  // [data-theme] picks one). This is the whole of the behaviour: flip the
  // attribute, arm the cross-fade for the length of the switch, persist.
  const themeBtn = $("#theme-toggle");
  if (themeBtn) {
    const paint = () => {
      const light = document.documentElement.dataset.theme === "light";
      themeBtn.title = light ? "切换到暗色主题" : "切换到亮色主题";
    };
    let settle = 0;
    themeBtn.onclick = () => {
      const root = document.documentElement;
      const next = root.dataset.theme === "light" ? "dark" : "light";
      const flip = () => {
        if (next === "dark") delete root.dataset.theme;
        else root.dataset.theme = "light";
        try { localStorage.setItem(LS_THEME, next); } catch (_) {}
        paint();
      };
      // A View Transition turns the switch into a diagonal wipe: the browser
      // freezes the page, we flip the attribute, and the new palette is
      // revealed over the old snapshot (style.css, ::view-transition-new).
      // The whole animation lives in CSS — this only says WHEN the DOM
      // changes, which is the one thing CSS cannot know.
      //
      // prefers-reduced-motion is NOT consulted, and it was at first; see the
      // note in style.css for why it came back out. The fallback below is now
      // only for a browser with no startViewTransition (Firefox shipped it
      // well after Chrome), and for a hidden document — a background tab
      // aborts the transition with InvalidStateError, so the flip has to
      // happen anyway.
      if (typeof document.startViewTransition === "function" &&
          document.visibilityState === "visible") {
        document.startViewTransition(flip);
        return;
      }
      // The cross-fade is armed only around the switch — left standing, it
      // makes every hover and the streaming caret lag (style.css .theming).
      root.classList.add("theming");
      clearTimeout(settle);
      settle = setTimeout(() => root.classList.remove("theming"), 380);
      flip();
    };
    paint();
  }
  $("#send").onclick = (e) => {
    if (!S.owns) return;                    // not replying HERE: let the form submit
    e.preventDefault();
    cancelTurn();
  };

  const input = $("#input");
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    // Ignore Enter while an IME is composing — that Enter confirms a candidate
    // (pinyin / CJK), and sending on it both fires a half-typed message and
    // leaves residue in the box. `isComposing` is the standard flag; keyCode
    // 229 is the legacy fallback some browsers still report instead.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send();
    }
  });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); if (!S.owns) send(); });
}

boot();
