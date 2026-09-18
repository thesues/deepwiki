// A project page must not reopen another project's conversation.
//
// Reported from production with a screenshot: the header said
// 代码理解·autumn-rs, the sidebar was empty — correctly, nothing is pinned to
// that project — and the transcript on screen was a 佛典 conversation about
// the 金刚经, with "另一个会话仍在回复中" above it.
//
// Two causes, both here:
//   1. `hermes.view` was ONE key per browser. Whatever was last read anywhere
//      came back on whichever project page opened next.
//   2. boot() checked that id against the UNFILTERED row list (every
//      project's sessions) while the sidebar filtered by project at render.
//      Two definitions of "what this page may show", and the drift between
//      them is the bug.
//
// Replays boot()'s rule as the page applies it. The server-side half — a send
// from that screen re-pinning the conversation — is pinned in
// test_app_routes.py.
import assert from "node:assert";

// as app.js defines them
const LS_VIEW = "hermes.view";
const viewKey = (S) => (S.profile ? `${LS_VIEW}.${S.profile}` : LS_VIEW);
function belongsHere(S, id) {
  if (!S.profile) return true;
  const row = (S.sessionRows || []).find((r) => r.id === id);
  if (!row) return false;
  return (row.profile || S.defaultProfile) === S.profile;
}
// as boot() decides
const reopens = (S, view) => (view && belongsHere(S, view) ? "open" : "fresh");

// The store as /api/sessions returns it: EVERY project's conversations, which
// is what the sidebar filters and what the recall used to trust unfiltered.
const rows = [
  { id: "jin-gang", profile: "buda", title: "讲一个金刚经里面的故事" },
  { id: "no-pin", profile: null, title: "旧会话" },        // files under the default
  { id: "rust", profile: "code-autumn-rs", title: "autumn-rs" },
];
const page = (key) => ({ profile: key, defaultProfile: "buda", sessionRows: rows });

// ── 1. the key is per project ───────────────────────────────────────────────
assert.strictEqual(viewKey(page("buda")), "hermes.view.buda");
assert.notStrictEqual(viewKey(page("code-autumn-rs")), viewKey(page("buda")),
  "two projects must not share the saved view; one key is how the 佛典 "
  + "conversation followed the reader onto the autumn-rs page");

// ── 2. the screenshot, as a test ────────────────────────────────────────────
assert.strictEqual(reopens(page("code-autumn-rs"), "jin-gang"), "fresh",
  "the autumn-rs page must NOT reopen a buda conversation — it opens on 新的对话");
assert.strictEqual(reopens(page("buda"), "jin-gang"), "open",
  "its own project still reopens it");

// ── 3. a live turn elsewhere does not drag its transcript here ─────────────
// This was the second escape: the old check also opened anything in
// S.streaming, regardless of project, which is why the page said "另一个会话
// 仍在回复中" while showing it. /api/sessions synthesizes a row for a turn
// that has not persisted yet, carrying the project it was started under, so
// the same predicate now covers it.
const withLive = {
  profile: "code-autumn-rs", defaultProfile: "buda",
  sessionRows: [...rows, { id: "live", profile: "buda", title: "回复中…" }],
};
assert.strictEqual(reopens(withLive, "live"), "fresh",
  "a turn running in another project is still another project's");

// ── 4. an unpinned session belongs to the DEFAULT project ──────────────────
assert.strictEqual(reopens(page("buda"), "no-pin"), "open",
  "no pin means the default project, which is where the sidebar files it too");
assert.strictEqual(reopens(page("code-autumn-rs"), "no-pin"), "fresh");

// ── 5. an id the page has never heard of is not opened ─────────────────────
assert.strictEqual(reopens(page("buda"), "deleted-elsewhere"), "fresh",
  "a stale saved view opens on 新的对话 rather than an empty transcript");

// ── 6. the home page (no project) is scoped to nothing ─────────────────────
assert.strictEqual(reopens({ profile: "", sessionRows: rows }, "jin-gang"), "open");

console.log("view scoping ok");
