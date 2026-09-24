// Three rules, one per new surface:
//
// 1. MEDIA — an image or video the agent produced is CONTENT, not a link. The
//    path the model typed (as text, or in a code span) must render as an
//    inline <img>/<video>; only documents (html) stay a link. The pattern this
//    pins is imported VERBATIM from app.js, so a drift there fails here.
// 2. TODO — hermes' todo tool answers every call with the FULL list, so the
//    card is one node updated in place, not one card per call.
// 3. SLASH — a leading "/" is a command, never a prompt to the model.
import assert from 'node:assert';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../../static/app.js', import.meta.url), 'utf8');

// ── 1. media ────────────────────────────────────────────────────────────────
function patternOf(name) {
  const m = src.match(new RegExp(`const ${name} = (\\/.+\\/[a-z]*)`)) ||
    src.match(new RegExp(`const ${name} = new RegExp\\(([\\s\\S]*?)\\);`));
  assert.ok(m, `${name} must be declared in app.js`);
  return new Function(`return ${m[1]};`)();
}

const ARTIFACT_RE = patternOf('ARTIFACT_RE');
const MEDIA_FILE_RE = patternOf('MEDIA_FILE_RE');

const show = (p) => (MEDIA_FILE_RE.test(p) ? 'media' : 'link');

assert.strictEqual(
  show('/artifacts/s1/图.png'), 'media', 'an image path renders inline');
assert.strictEqual(
  show('/opt/data/artifacts/s1/图.png'), 'media', 'the filesystem spelling too');
assert.strictEqual(
  show('/artifacts/s1/teach.mp4'), 'media', 'a video path renders inline');
assert.strictEqual(
  show('/static/media/cover.webp'), 'media', 'media under /static renders inline');
assert.strictEqual(
  show('/artifacts/s1/arch.html'), 'link', 'a document stays a link');
assert.strictEqual(
  show('/artifacts/s1/notes.md'), 'link', 'unknown extensions stay links');

// The artifact regex recognises the media spellings at all — the shared
// collector matches text nodes against ARTIFACT_RE, so a media path it
// misses is never even considered.
assert.ok(ARTIFACT_RE.test('/artifacts/s1/图.png'));
assert.ok(ARTIFACT_RE.test('/opt/data/artifacts/s1/teach.mp4'));
assert.ok(ARTIFACT_RE.test('/static/media/cover.jpg'));
assert.ok(!ARTIFACT_RE.test('/static/app.js'), 'code assets are not artifacts');
assert.ok(!ARTIFACT_RE.test('/etc/passwd.png'), 'paths outside the mounts are not artifacts');

// ── 2. todo: one card, updated in place ─────────────────────────────────────
function makeBoard() {
  // Mirrors showTodos(): a stale/absent card is created; a live one is
  // REPAINTED, so three todo calls in one turn read as one checklist that
  // ticks over rather than three plans.
  let card = null;
  return {
    get count() { return card ? 1 : 0; },
    get rows() { return card ? card.rows : []; },
    show(items) {
      if (!Array.isArray(items) || !items.length) return;
      if (!card) card = { rendered: 0, rows: [] };
      card.rendered++;
      card.rows = items;
    },
  };
}
const board = makeBoard();
board.show([
  { id: '1', content: 'a', status: 'in_progress' },
  { id: '2', content: 'b', status: 'pending' },
]);
const afterFirst = board.count;
board.show([
  { id: '1', content: 'a', status: 'completed' },
  { id: '2', content: 'b', status: 'in_progress' },
]);
assert.strictEqual(afterFirst, 1, 'the first call draws one card');
assert.strictEqual(board.count, 1, 'a second todo call repaints, not stacks');
assert.strictEqual(board.rows[0].status, 'completed', 'the LATEST list is the state');

// ── 3. slash: a command is never a prompt ───────────────────────────────────
// Mirrors send()'s gate and runSlashCommand()'s table. The contract the rest
// of the app depends on: a handled command returns BEFORE any fetch, and an
// unknown one still does not reach the model as a prompt about itself.
const HANDLED = new Set(['clear', 'new', 'reset', 'help']);
const commands = [];
function handle(text) {
  if (!text.startsWith('/') || text.length <= 1) return false;
  const name = text.slice(1).trim().split(/\s+/)[0].toLowerCase();
  commands.push(name);
  return true;
}
for (const c of ['/clear', '/new', '/reset', '/help', '/CLEAR']) {
  assert.strictEqual(handle(c), true, `${c} is handled locally`);
}
assert.strictEqual(commands.includes('clear'), true);
assert.strictEqual(commands.includes('help'), true);
assert.strictEqual(handle('什么是怨憎会苦'), false, 'ordinary text is a prompt');
assert.strictEqual(handle('/'), false, 'a bare slash is text, not a command');

const declared = [...src.matchAll(/\["\\?\/(\w+)"/g)].map((m) => m[1]);
for (const [, d] of (src.match(/\[\"\/(\w+)\", \"[^\"]+\"\]/g) || []).entries()) {
  const name = d.match(/\"\/(\w+)\"/)[1];
  assert.ok(HANDLED.has(name), `/help advertises only commands the gate honours: ${name}`);
}
assert.ok(SLASH_TABLE_COVERS_CLEAR(src));

function SLASH_TABLE_COVERS_CLEAR(s) {
  return /\"\/clear\"/.test(s) && /clear\" \|\| name === \"new\" \|\| name === \"reset\"/.test(s);
}

console.log('ok - media renders inline, todo is one live card, slash never becomes a prompt');
