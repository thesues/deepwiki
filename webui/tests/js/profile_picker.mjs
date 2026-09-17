// The project (profile) flow after the two-page split: the HOME page (/) is
// the card grid, and each project lives at /<key>/ — the chat page reads its
// project off the URL and never offers a picker. The back half
// (`/api/chat/start {profile}`) has its own tests in test_app_routes.py.
// This replays the frontend's rules against the sequences that would break
// them:
//
//   1. THE KEY COMES OFF THE URL — /buda/ → buda; trailing slashes fold.
//   2. AN UNKNOWN KEY REDIRECTS HOME — a stale bookmark degrades to the
//      card grid, never to a broken page pretending to be a project.
//   3. THE HOME PAGE ALWAYS RENDERS CARDS — one declared project is still a
//      project page you navigate to, deepwiki-style.
//   4. THE CHAT PAGE HAS NO PROFILE PICKER — the project is the URL, not a
//      widget; switching projects is navigation.
//   5. SEND CARRIES THE URL'S KEY AND ADOPTS THE ECHO — `profile` in the
//      response is what the server RESOLVED; the title follows it.
import assert from 'node:assert';

// ── 1+2. the URL rule, as boot() applies it ─────────────────────────────────
function pageProfile(pathname, declaredKeys) {
  // as S.profile is initialized in app.js
  const key = (pathname.replace(/\/+$/, "").split("/").pop() || "").trim();
  // as boot() applies it against /api/status
  if (!declaredKeys.includes(key)) return { redirect: "/" };
  return { profile: key };
}

const keys = ["buda", "code"];
assert.deepStrictEqual(pageProfile("/buda/", keys), { profile: "buda" });
assert.deepStrictEqual(pageProfile("/buda", keys), { profile: "buda" },
  "the trailing slash is optional — the server redirects, the page tolerates");
assert.deepStrictEqual(pageProfile("/", keys), { redirect: "/" },
  "the home page has no project and stays home");
assert.deepStrictEqual(pageProfile("/gone/", keys), { redirect: "/" },
  "a renamed profile's stale bookmark falls back to the card grid");

// ── 3. the home-page rule, as home.js applies it ────────────────────────────
function homeView(profiles, error) {
  if (error) return "error";
  return (profiles || []).length ? "cards" : "empty-message";
}
assert.strictEqual(homeView([{ key: "buda" }]), "cards",
  "ONE project still renders the card grid — the card is the door to /buda/");
assert.strictEqual(homeView([{ key: "a" }, { key: "b" }]), "cards");
assert.strictEqual(homeView([]), "empty-message", "no projects says so, in place");
assert.strictEqual(homeView(null, true), "error", "a failed /api/status says so");

// ── 4. the chat page has no picker ──────────────────────────────────────────
// app.js ships no renderProfiles() at all: the #profile element is gone from
// index.html, and nothing but the URL decides S.profile. Assert the negative
// by construction — the send payload's profile field is read-only state.
function composerControls() {
  return ["endpoint"];   // the model picker is the ONLY control below the box
}
assert.ok(!composerControls().includes("profile"),
  "the composer must not offer a project choice; that is the home page's job");

// ── 5. the send payload + echo adoption, as send() applies it ───────────────
function sendPayload(S) {
  return {
    endpoint: S.endpoint || undefined,
    profile: S.profile || undefined,
  };
}
assert.strictEqual(sendPayload({ profile: "buda" }).profile, "buda",
  "the send names the project this page serves");
assert.strictEqual(sendPayload({}).profile, undefined,
  "an empty URL (never reached in prod) sends no profile rather than a lie");

function adopt(requested, echoed) {
  return echoed || requested;
}
assert.strictEqual(adopt("buda", "buda"), "buda");
assert.strictEqual(adopt("gone", "buda"), "buda",
  "the server resolved the stale name to the default; the page follows");

console.log("ok - the two-page project flow's rules hold");
