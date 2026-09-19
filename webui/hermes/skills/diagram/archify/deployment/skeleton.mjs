#!/usr/bin/env node
// Rename a validated skeleton without ever reading its geometry.
//
// The agent that uses this skill cannot see a skeleton whole: tool output is
// capped at 3,500 characters here and the examples run 4-8 KB, so `cat` hands
// back a truncated file whose tail — boundaries, meta.views — is exactly the
// part a rename must not miss. Re-emitting all that geometry through the model
// would be worse: thousands of tokens of coordinates transcribed by hand, to
// arrive back where it started.
//
// So the geometry never leaves this process. `inventory` prints what can be
// renamed, small enough to read in one go; `rename` applies a map and writes
// the result. The model supplies names, not numbers.
//
// Usage:
//   node deployment/skeleton.mjs inventory examples/web-app.architecture.json
//   node deployment/skeleton.mjs rename examples/web-app.architecture.json map.json out.json
//
// map.json:
//   {
//     "title": "autumn-rs 读路径",
//     "components": {
//       "cdn": { "id": "fuse", "label": "autumn-fuse", "sublabel": "POSIX 挂载" },
//       "lb":  { "id": "manager", "label": "manager" }
//     },
//     "connections": { "cdn-to-lb": { "label": "lookup" } },
//     "drop": ["s3", "queue"]
//   }
import { readFileSync, writeFileSync } from 'node:fs';

const [, , cmd, file, mapFile, outFile] = process.argv;
const die = (msg) => { console.error(msg); process.exit(1); };
if (!cmd || !file) die('usage: skeleton.mjs inventory <skeleton.json> | rename <skeleton.json> <map.json> <out.json>');

const doc = JSON.parse(readFileSync(file, 'utf8'));
const items = doc.components || doc.steps || doc.participants || doc.stages || doc.states || [];
const links = doc.connections || doc.transitions || doc.messages || doc.edges || [];

if (cmd === 'inventory') {
  console.log(`type: ${doc.diagram_type || '?'}  schema_version: ${doc.schema_version ?? 1}`);
  console.log(`title: ${doc.meta?.title ?? ''}`);
  console.log(`\n${items.length} renameable nodes (id | type | label):`);
  for (const c of items) console.log(`  ${c.id} | ${c.type ?? ''} | ${c.label ?? ''}${c.sublabel ? ' / ' + c.sublabel : ''}`);
  if (doc.boundaries?.length) {
    console.log(`\n${doc.boundaries.length} boundaries (label -> wraps):`);
    for (const b of doc.boundaries) console.log(`  ${b.label} -> ${(b.wraps || []).join(', ')}`);
  }
  console.log(`\n${links.length} connections (id | from -> to | label):`);
  for (const l of links) console.log(`  ${l.id ?? '-'} | ${l.from ?? l.source ?? '?'} -> ${l.to ?? l.target ?? '?'} | ${l.label ?? ''}`);
  if (doc.meta?.views?.length) console.log(`\n${doc.meta.views.length} guided views — ids inside them are renamed for you.`);
  console.log('\nGeometry (pos/size/route/via/labelAt) stays as it is. Rename ids and labels only.');
  process.exit(0);
}

if (cmd !== 'rename') die(`unknown command "${cmd}"`);
if (!mapFile || !outFile) die('rename needs <map.json> <out.json>');
const map = JSON.parse(readFileSync(mapFile, 'utf8'));
const compMap = map.components || {};
const dropped = new Set(map.drop || []);
const idFor = new Map(Object.entries(compMap).map(([from, to]) => [from, to.id || from]));

const known = new Set(items.map((c) => c.id));
for (const id of [...Object.keys(compMap), ...dropped]) {
  if (!known.has(id)) die(`"${id}" is not in ${file}. Run: node deployment/skeleton.mjs inventory ${file}`);
}

// 1. drop, 2. rename+relabel, 3. rewrite every remaining reference by value.
const keep = items.filter((c) => !dropped.has(c.id));
for (const c of keep) {
  const m = compMap[c.id];
  if (!m) continue;
  if (m.label) c.label = m.label;
  if (m.sublabel !== undefined) { if (m.sublabel) c.sublabel = m.sublabel; else delete c.sublabel; }
  if (m.type) c.type = m.type;
  if (m.tag !== undefined) { if (m.tag) c.tag = m.tag; else delete c.tag; }
  if (m.id) c.id = m.id;
}
if (doc.components) doc.components = keep; else if (doc.steps) doc.steps = keep;

const live = new Set(keep.map((c) => c.id));
const rename = (v) => (typeof v === 'string' && idFor.has(v) ? idFor.get(v) : v);
const endpointGone = (l) => {
  const [a, b] = [l.from ?? l.source, l.to ?? l.target];
  return dropped.has(a) || dropped.has(b);
};
const keptLinks = links.filter((l) => !endpointGone(l));
for (const l of keptLinks) {
  for (const k of ['from', 'to', 'source', 'target']) if (l[k] !== undefined) l[k] = rename(l[k]);
  const lm = (map.connections || {})[l.id];
  if (lm?.label !== undefined) { if (lm.label) l.label = lm.label; else delete l.label; }
}
if (doc.connections) doc.connections = keptLinks; else if (doc.transitions) doc.transitions = keptLinks;

// Everything else that names a component: boundaries[].wraps, meta.views[].focus,
// cards, anywhere. Walk by value rather than by path — a path list is a thing to
// keep in sync with upstream, and this is not.
const walk = (node) => {
  if (Array.isArray(node)) return node.map(walk).filter((v) => !(typeof v === 'string' && dropped.has(v)));
  if (node && typeof node === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(node)) out[k] = walk(v);
    return out;
  }
  return rename(node);
};
for (const k of ['boundaries', 'meta', 'cards', 'layout']) if (doc[k] !== undefined) doc[k] = walk(doc[k]);
if (map.title) doc.meta = { ...(doc.meta || {}), title: map.title };
delete doc.meta?.output;

writeFileSync(outFile, JSON.stringify(doc, null, 1));
const orphanWraps = (doc.boundaries || []).filter((b) => (b.wraps || []).some((w) => !live.has(w)));
console.log(`wrote ${outFile}`);
console.log(`  nodes ${items.length} -> ${keep.length}${dropped.size ? ` (dropped ${[...dropped].join(', ')})` : ''}`);
console.log(`  connections ${links.length} -> ${keptLinks.length}`);
console.log(`  renamed ${idFor.size} id(s); geometry untouched`);
if (orphanWraps.length) console.log(`  NOTE: boundaries still wrap unknown ids — ${orphanWraps.map((b) => b.label).join('; ')}`);
console.log(`next: node bin/archify.mjs validate ${doc.diagram_type || 'architecture'} ${outFile} --json`);
