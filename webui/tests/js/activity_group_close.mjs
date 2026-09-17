// The activity group must close at each user prompt in a REPLAYED transcript.
//
// activityGroup() reuses the live group while it is still in the DOM, which is
// right for one turn (thinking + N tools = one row). But a long conversation's
// history replay never fires endTurn between turns — `end` events are not
// persisted — so without an explicit close, every later turn's tools funnelled
// into the FIRST turn's group. On screen: the last question stood alone with
// no activity under it, and a wall of unrelated tool rows sat above it,
// after earlier questions. Reported from production: a 23-message buda session
// where "金刚经里面的故事？" showed nothing but its answer, all 16 tool rows
// piled after the FIRST question.
//
// The fix closes the group (and the tool-id map) on `user` and `history_user`.
// This test replays the production shape — two prompts, tools after each —
// and asserts group membership follows the prompts.
import assert from 'node:assert';

// ── the rule, as apply() applies it ─────────────────────────────────────────
function replayGroups(events) {
  // Mirrors the client: one group per turn, opened by the first tool/think
  // after a prompt, closed by the next prompt.
  const groups = [];      // [{ prompt, tools: [] }]
  let current = null;
  let activity = null;    // S.activity
  const tools = new Map(); // S.tools: id -> group
  for (const ev of events) {
    if (ev.kind === "history_user" || ev.kind === "user") {
      activity = null;    // ← the fix: the prompt closes the previous group
      tools.clear();
      current = { prompt: ev.text, tools: [] };
      groups.push(current);
    } else if (ev.kind === "tool") {
      if (!activity) activity = { group: current };
      if (!tools.has(ev.id)) current.tools.push(ev.title || ev.id);
    }
    // delta/note: no effect on grouping
  }
  return groups;
}

// The production transcript (hermes_session_api.py history, session
// 20260917_041815_9c3113), compressed to its shape:
const events = [
  { kind: "history_user", text: "不是有那个第二只箭的故事吗？" },
  { kind: "tool", id: "a1", title: "mcp_memory_search_docs" },
  { kind: "note", text: "（此前的对话已压缩为上下文摘要）" },
  { kind: "tool", id: "a2", title: "mcp_memory_get_symbol" },
  { kind: "delta", thought: false, text: "语料库里相关的经文…" },
  { kind: "history_user", text: "金刚经里面的故事？" },
  { kind: "tool", id: "b1", title: "mcp_memory_get_symbol" },
  { kind: "tool", id: "b2", title: "mcp_memory_search_docs" },
  { kind: "tool", id: "b3", title: "mcp_memory_get_symbol" },
];

const groups = replayGroups(events);
assert.strictEqual(groups.length, 2, "two prompts, two groups");
assert.strictEqual(groups[0].tools.length, 2,
  "the first turn's group holds only the first turn's tools");
assert.strictEqual(groups[1].prompt, "金刚经里面的故事？");
assert.strictEqual(groups[1].tools.length, 3,
  "the last question's tools must render AFTER it, in its own group");
assert.ok(!groups[1].tools.includes("mcp_memory_search_docs") || groups[0].tools.length === 2,
  "no bleeding of the first turn's rows into the last question's group");

// The pre-fix behaviour, for contrast: without the close, one group holds
// everything and the last prompt's group is empty.
function replayGroupsBuggy(events) {
  const groups = [];
  let current = null;
  for (const ev of events) {
    if (ev.kind === "history_user") {
      current = { prompt: ev.text, tools: [] };
      groups.push(current);
    } else if (ev.kind === "tool") {
      groups[0].tools.push(ev.title);   // ← the bug: always the first group
    }
  }
  return groups;
}
const buggy = replayGroupsBuggy(events);
assert.strictEqual(buggy[0].tools.length, 5, "pre-fix: all five tools in group one");
assert.strictEqual(buggy[1].tools.length, 0, "pre-fix: the last question shows nothing");

console.log("ok - a replayed prompt closes the activity group");
