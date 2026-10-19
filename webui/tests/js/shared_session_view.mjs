import assert from "node:assert";

import fs from "node:fs";
import vm from "node:vm";
const source = fs.readFileSync(new URL("../../static/app.js", import.meta.url), "utf8");
const start = source.indexOf("function belongsHere(");
const fn = source.slice(start, source.indexOf("\n}", start) + 2);
function belongsHere(S, id) {
  const context = vm.createContext({ S, id });
  return vm.runInContext(fn + "\nbelongsHere(id)", context);
}
// as boot() decides
const reopens = (S, view) => (view && belongsHere(S, view) ? "open" : "fresh");


const rows = [
  { id: "buddhist", source: "deepwiki:buda" },
  { id: "tao", source: "deepwiki:tao" },
  { id: "code", source: "deepwiki:code-autumn-rs" },
  { id: "legacy" },
  { id: "live", is_streaming: true },
];
const state = { sessionRows: rows };
for (const row of rows) assert.strictEqual(reopens(state, row.id), "open");
assert.strictEqual(reopens(state, "deleted"), "fresh");
assert.strictEqual(reopens({ sessionRows: [] }, "buddhist"), "fresh");
console.log("shared session view ok");
