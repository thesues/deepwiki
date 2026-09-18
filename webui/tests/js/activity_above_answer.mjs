// The activity row belongs ABOVE the answer, in every event order.
//
// activityGroup() used to append the disclosure wherever it was created, which
// is faithful to the event stream and reads wrong the moment the answer comes
// first. Two shapes do that, both real:
//
//   1. A replayed assistant message that carries BOTH text and tool_calls.
//      hermes_session_api.history emits the message's text, then its own
//      calls — so "让我查一下语库" lands before the tools it announced, and the
//      group is pushed under it. Five such messages were in the store when
//      this was written.
//   2. A live turn that answers and then keeps calling tools.
//
// Either way the reader got the conclusion first and the work underneath. The
// fix records the turn's FIRST answer bubble (S.turnTop) and inserts the group
// in front of it; one group per turn, at the top of the turn, however many
// times the turn alternates.
import assert from 'node:assert';

// A transcript as the DOM holds it: an ordered list of rows.
function place(events) {
  const rows = [];            // ["answer"|"activity"|"user", …]
  let activity = null;        // S.activity
  let turnTop = null;         // S.turnTop — index of this turn's first answer
  for (const ev of events) {
    if (ev === "user") {
      activity = null; turnTop = null;   // a prompt opens a new turn
      rows.push("user");
    } else if (ev === "tool" || ev === "think") {
      if (activity) continue;            // one group per turn
      activity = "activity";
      if (turnTop !== null) rows.splice(turnTop, 0, "activity");
      else rows.push("activity");
    } else if (ev === "answer") {
      rows.push("answer");
      if (turnTop === null) turnTop = rows.length - 1;
    }
  }
  return rows;
}

// The ordinary shape: tools first. Unchanged by the fix.
assert.deepStrictEqual(
  place(["user", "tool", "tool", "answer"]),
  ["user", "activity", "answer"],
);

// Shape 1 — replayed message carrying both text and calls: the text arrives
// before the calls it announced, and the group still has to come out on top.
assert.deepStrictEqual(
  place(["user", "answer", "tool", "tool"]),
  ["user", "activity", "answer"],
);

// Shape 2 — answer, more tools, another answer. ONE group, above both.
assert.deepStrictEqual(
  place(["user", "answer", "tool", "answer"]),
  ["user", "activity", "answer", "answer"],
);

// Two turns stay independent: the second turn's group must not migrate into
// the first, and must sit above its own answer.
assert.deepStrictEqual(
  place(["user", "answer", "tool", "user", "answer", "tool"]),
  ["user", "activity", "answer", "user", "activity", "answer"],
);

// Thinking alone opens the group too, and is subject to the same rule.
assert.deepStrictEqual(
  place(["user", "answer", "think"]),
  ["user", "activity", "answer"],
);

console.log("ok");
