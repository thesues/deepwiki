// The theme toggle, deepwiki's shape: an icon at the far right of the header,
// and a switch that is CSS all the way down.
//
// Runs the REAL `static/home.js` in a vm over a DOM just large enough for it,
// and CLICKS the button — the point of the rewrite was to move the icon choice
// out of script and into `[data-theme]`, and only pressing the thing shows
// whether the script still does the one job it kept. Pins:
//
//   1. THE CLICK FLIPS ONE ATTRIBUTE — dark is the ABSENCE of data-theme
//      (the original look is the default), light sets it. Not a class, not a
//      second source of truth: the inline pre-paint script in <head> reads
//      exactly this, and the CSS selects on it.
//   2. THE SCRIPT NEVER PICKS THE ICON — both sun and moon live in the
//      markup. A `textContent = "☀"` anywhere here is the old design, and it
//      would silently overwrite the two <svg> children.
//   3. THE CROSS-FADE IS ARMED, THEN DISARMED — `.theming` is what makes the
//      palette fade instead of snap, and leaving it on makes every hover in
//      the app lag behind the pointer.
//   4. THE CHOICE PERSISTS under the key the <head> script reads.
//   5. BOTH PAGES RUN THE SAME TOGGLE — the block is duplicated in app.js and
//      home.js (two bundles, no shared module), so the test compares them and
//      fails if one is fixed without the other.
import assert from "node:assert";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.join(here, "..", "..", "static");
const HOME = fs.readFileSync(path.join(STATIC, "home.js"), "utf8");
const APP = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

/* ---------- a DOM just large enough for home.js ---------- */
class Node_ {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase();
    this.children = []; this.className = ""; this.id = ""; this.dataset = {};
    this.hidden = false; this.title = ""; this.onclick = null; this._text = "";
    this.href = ""; this.classList = new Set_(this);
  }
  get textContent() { return this._text; }
  set textContent(t) { this._text = t == null ? "" : String(t); this.wroteText = true; }
  appendChild(n) { this.children.push(n); return n; }
  append(...ns) { ns.forEach((n) => this.appendChild(n)); }
}
class Set_ {
  constructor(n) { this.n = n; this.s = new Set(); }
  add(...c) { c.forEach((x) => this.s.add(x)); this.n.className = [...this.s].join(" "); }
  remove(...c) { c.forEach((x) => this.s.delete(x)); this.n.className = [...this.s].join(" "); }
  contains(c) { return this.s.has(c); }
}

function run({ saved = null } = {}) {
  const byId = new Map();
  ["cards", "home-error", "build", "theme-toggle"].forEach((id) => {
    const n = new Node_(id === "theme-toggle" ? "button" : "div"); n.id = id; byId.set(id, n);
  });
  // The <html> element: `documentElement.dataset.theme` is the whole of the
  // theme state, and `classList` on it is where the cross-fade is armed.
  const html = new Node_("html");
  const store = new Map();
  if (saved !== null) store.set("hermes.theme", saved);
  // The pre-paint script in <head>, replayed: it is what makes a reload open
  // in the theme the reader chose, and the button must agree with it.
  if (store.get("hermes.theme") === "light") html.dataset.theme = "light";

  const timers = [];
  const ctx = vm.createContext({
    document: {
      documentElement: html,
      getElementById: (id) => byId.get(id) || null,
      createElement: (t) => new Node_(t),
    },
    // The two lists the page draws its cards from. Resolved, so boot() gets
    // all the way to the toggle it wires at the end.
    fetch: (url) => Promise.resolve({
      json: async () => (String(url).includes("status")
        ? { profiles: [{ key: "code", label: "代码索引" }], defaultProfile: "code" }
        : { sessions: [] }),
    }),
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (h) => { if (timers[h - 1]) timers[h - 1].cancelled = true; },
    console, JSON, Promise, Map, Set, Object, Array, String, Number, encodeURIComponent,
  });
  vm.runInContext(HOME, ctx, { filename: "home.js" });
  return { html, store, timers, btn: byId.get("theme-toggle") };
}

// boot() is async (it awaits the two lists); let its microtasks drain.
const settled = () => new Promise((r) => setTimeout(r, 0));

/* ── 1+2+4. dark → light → dark, on the attribute alone ──────────────────── */
{
  const { html, store, btn } = run();
  await settled();
  assert.strictEqual(html.dataset.theme, undefined,
    "dark is the DEFAULT and the absence of the attribute, not data-theme=dark");
  assert.strictEqual(btn.title, "切换到亮色主题",
    "the button names where a click GOES, which is what makes it read as an action");
  assert.ok(!btn.wroteText,
    "the script must not write the icon — sun and moon are both in the markup, "
    + "and textContent would delete them");

  btn.onclick();
  assert.strictEqual(html.dataset.theme, "light", "the click sets the light attribute");
  assert.strictEqual(store.get("hermes.theme"), "light", "and persists the choice");
  assert.strictEqual(btn.title, "切换到暗色主题", "the title now names the way back");
  assert.ok(!btn.wroteText, "still no icon written by script");

  btn.onclick();
  assert.strictEqual(html.dataset.theme, undefined,
    "back to dark REMOVES the attribute — a data-theme=dark would make the "
    + "[data-theme=light] rules and the <head> script disagree about the default");
  assert.strictEqual(store.get("hermes.theme"), "dark");
}

/* ── the saved choice is what the button starts from ─────────────────────── */
{
  const { html, btn } = run({ saved: "light" });
  await settled();
  assert.strictEqual(html.dataset.theme, "light", "a light reader reloads into light");
  assert.strictEqual(btn.title, "切换到暗色主题");
  btn.onclick();
  assert.strictEqual(html.dataset.theme, undefined, "and can leave it");
}

/* ── 3. the cross-fade is armed for the switch and then removed ──────────── */
{
  const { html, btn, timers } = run();
  await settled();
  assert.ok(!html.classList.contains("theming"),
    "no standing transition: it would make every hover and the streaming caret lag");
  btn.onclick();
  assert.ok(html.classList.contains("theming"), "the click arms the palette cross-fade");
  const armed = timers.filter((t) => !t.cancelled);
  assert.strictEqual(armed.length, 1, "exactly one disarm is scheduled");
  assert.ok(armed[0].ms >= 300, "it outlasts the .3s transition it is arming");
  armed[0].fn();
  assert.ok(!html.classList.contains("theming"), "and it is taken off again");

  // Two clicks in a row must not leave the first disarm to fire mid-second
  // switch — that is what clearTimeout is for.
  btn.onclick(); btn.onclick();
  assert.strictEqual(timers.filter((t) => !t.cancelled).length, 1,
    "a rapid double switch keeps ONE pending disarm, not a queue of them");
}

/* ── 5. the two pages cannot drift ───────────────────────────────────────── */
{
  const grab = (src, name) => {
    const i = src.indexOf('themeBtn.onclick = () => {');
    assert.ok(i > 0, `${name} must wire the toggle`);
    const end = src.indexOf("\n    };", i);
    return src.slice(i, end).replace(/LS_THEME/g, '"hermes.theme"');
  };
  assert.strictEqual(grab(APP, "app.js"), grab(HOME, "home.js"),
    "the chat page and the home page must switch the theme the same way; "
    + "they are two bundles, so the block is duplicated and has to be kept equal");
  for (const [name, src] of [["app.js", APP], ["home.js", HOME]]) {
    assert.ok(!/themeBtn\.textContent/.test(src),
      `${name} must not write the toggle's text: the icons are markup + CSS now`);
  }
}

console.log("theme toggle ok");
