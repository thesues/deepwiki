"""What a tool call looks like AFTER a reload.

The reported bug was that a tool row showed nothing useful. The live path was
fixed first, and a review caught that the fix stopped at the live path: a reader
who refreshed got the raw `{"output":…,"exit_code":…}` back, with no command
above it and a failure rendered as a success. These pin the replayed shape
against the messages hermes actually persists -- captured from the store, not
invented:

  call:   {"id": "call_terminal_0_…", "function": {"name": "terminal",
           "arguments": '{"command":"echo HELLO-OK","timeout":15}'}}
  result: {"role": "tool", "tool_call_id": "call_terminal_0_…",
           "tool_name": "terminal",
           "content": '{"output": "HELLO-OK", "exit_code": 0, "error": null}'}

`arguments` is a JSON STRING and `is_error` is NOT persisted -- both shape what
replay can and cannot say.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hermes_session_api as hs  # noqa: E402


def _msgs(command, output, exit_code, error=None, extra_args=None):
    import json

    args = {"command": command}
    if extra_args:
        args.update(extra_args)
    return [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "call_id": "call_1", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps(args)},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "terminal",
         "content": json.dumps({"output": output, "exit_code": exit_code,
                                "error": error})},
    ]


def _replay(monkeypatch, msgs):
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: msgs)})())
    return [e for e in hs.history("s", 0) if e.get("kind") == "tool"]


def test_a_reloaded_row_shows_the_command_and_the_output(monkeypatch):
    """Ablation: drop the `arguments` parse and the command disappears; drop
    `_detail_for` and the row is raw JSON again."""
    rows = _replay(monkeypatch, _msgs("echo HELLO-OK", "HELLO-OK", 0))
    assert "echo HELLO-OK" in rows[0]["detail"], "the call row lost its command"
    assert "echo HELLO-OK" in rows[1]["detail"], "the result row lost its command"
    assert "HELLO-OK" in rows[1]["detail"]
    assert rows[0]["id"] == rows[1]["id"] == "call_1"


def test_a_second_argument_does_not_turn_the_command_into_json(monkeypatch):
    """`terminal` persists a `timeout` beside the command, so the arguments dict
    has two keys. A rule that only unwrapped SINGLE-valued dicts printed the
    whole JSON — the live row said `echo …` and the reloaded one said
    `{"command": "echo …", "timeout": 15}` for the same call.

    Falls back to the dict rendering without hermes installed, which is what
    this test environment has; the assertion is that the COMMAND is legible
    either way."""
    rows = _replay(monkeypatch,
                   _msgs("echo HELLO-OK", "HELLO-OK", 0, extra_args={"timeout": 15}))
    assert "echo HELLO-OK" in rows[1]["detail"]


def test_a_nonzero_exit_survives_the_reload(monkeypatch):
    rows = _replay(monkeypatch, _msgs("ls /nope", "No such file or directory", 2))
    assert "exit 2" in rows[1]["detail"]
    assert "No such file or directory" in rows[1]["detail"]


def test_an_explicit_error_is_the_only_failure_replay_can_claim(monkeypatch):
    """`is_error` is not persisted, so hermes' verdict cannot be recovered. An
    explicit `error` in the payload is a failure by any reading; a non-zero exit
    alone stays `completed` with the code visible, because `grep` answers 1 for
    "no match" and inventing a failure is the bigger lie."""
    plain = _replay(monkeypatch, _msgs("ls /nope", "nope", 2))
    assert plain[1]["status"] == "completed"
    erred = _replay(monkeypatch, _msgs("boom", "", 1, error="tool exploded"))
    assert erred[1]["status"] == "failed"
    assert "tool exploded" in erred[1]["detail"]


def test_the_true_length_is_a_number_not_the_text(monkeypatch):
    """The client computes "还有 N 字" from `detailFull`. Sending the string made
    that arithmetic NaN on every replayed result."""
    rows = _replay(monkeypatch, _msgs("cat big", "x" * 9000, 0))
    assert isinstance(rows[1]["detailFull"], int)
    assert len(rows[1]["detail"]) <= hs.DETAIL_MAX
    assert rows[1]["detailFull"] > len(rows[1]["detail"])


def test_a_call_whose_result_was_never_stored_is_not_left_running(monkeypatch):
    """Captured from production (session "哦"): an assistant message calls
    `mcp_memory_search_docs`, no `tool` row follows, and the final answer comes
    next. Replayed as `pending`, the row kept a live timer ticking and put a
    思考中 row under an idle transcript. It is `incomplete` — never `pending`,
    and never `completed`, which would claim a result nobody stored."""
    msgs = [
        {"role": "user", "content": "为什么拈花"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_mcp_0", "type": "function",
            "function": {"name": "mcp_memory_search_docs",
                         "arguments": '{"query": "拈花微笑"}'},
        }]},
        {"role": "assistant", "content": "语料库里关于拈花微笑的记载……"},
    ]
    rows = _replay(monkeypatch, msgs)
    assert [r["status"] for r in rows] == ["incomplete"], rows
    # A call that DID get its result keeps the pairing: the call row may be
    # provisional, the result row completes it.
    answered = _replay(monkeypatch, _msgs("echo ok", "ok", 0))
    assert answered[-1]["status"] == "completed"
    assert all(r["status"] != "incomplete" for r in answered), answered


# ── compaction summaries must not render as speech ─────────────────────────

def test_history_compaction_summary_becomes_a_note_not_a_message():
    """hermes persists a `[CONTEXT COMPACTION — REFERENCE ONLY]…` instruction
    block as an ordinary user row after compressing history. The transcript
    rendered it as if someone had said it — a wall of highlighted prose nobody
    wrote. It is a note now."""
    import hermes_session_api as hsa

    class FakeDB:
        def get_messages(self, sid):
            return [
                {"role": "user", "content":
                 "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were "
                 "compacted into the summary below. " + "x" * 500},
                {"role": "user", "content": "金刚经里面的故事?"},
                {"role": "assistant", "content": "好的，讲一个故事"},
            ]

    orig = hsa._db
    hsa._db = lambda: FakeDB()
    try:
        events = hsa.history("s", 100)
    finally:
        hsa._db = orig
    assert [e["kind"] for e in events] == ["note", "history_user", "delta"]
    assert events[0]["text"] == "（此前的对话已压缩为上下文摘要）"


def test_history_legacy_summary_prefix_also_becomes_a_note():
    import hermes_session_api as hsa

    class FakeDB:
        def get_messages(self, sid):
            return [{"role": "assistant", "content": "[CONTEXT SUMMARY]: earlier stuff"}]

    orig = hsa._db
    hsa._db = lambda: FakeDB()
    try:
        events = hsa.history("s", 100)
    finally:
        hsa._db = orig
    assert [e["kind"] for e in events] == ["note"]


def test_history_real_user_message_with_similar_opening_is_kept():
    """Only whole-summary rows are skipped. A human message that merely quotes
    or mentions compaction is speech and must survive."""
    import hermes_session_api as hsa

    class FakeDB:
        def get_messages(self, sid):
            return [{"role": "user", "content": "什么是 context compaction？"}]

    orig = hsa._db
    hsa._db = lambda: FakeDB()
    try:
        events = hsa.history("s", 100)
    finally:
        hsa._db = orig
    assert [e["kind"] for e in events] == ["history_user"]


def test_history_replays_the_full_compression_lineage(monkeypatch):
    """The sidebar displays the compression tip, not just its final fragment."""
    import sqlite3

    class FakeDB:
        def __init__(self):
            self._conn = sqlite3.connect(":memory:")
            self._conn.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)"
            )
            self._conn.executemany(
                "INSERT INTO sessions VALUES (?, ?)",
                [("root", None), ("middle", "root"), ("tip", "middle")],
            )

        @staticmethod
        def get_messages(sid):
            return [{"role": "user", "content": sid}]

    monkeypatch.setattr(hs, "_db", lambda: FakeDB())
    events = hs.history("tip", 0)
    assert [event["text"] for event in events] == ["root", "middle", "tip"]


# ── the todo checklist, replayed ────────────────────────────────────────────
#
# A reload must show the same checklist a live reader saw: the tool row with a
# one-line summary, then a `todo` event carrying the plain items. Without the
# event, a plan the agent rewrote three times mid-turn survives the reload as
# nothing but a collapsed JSON row — the one shape a plan must not take.


def _todo_msgs():
    import json

    result = json.dumps({
        "todos": [
            {"id": "1", "content": "检索经文", "status": "completed"},
            {"id": "2", "content": "整理引文", "status": "in_progress"},
        ],
        "summary": {"total": 2, "completed": 1},
    }, ensure_ascii=False)
    return [
        {"role": "user", "content": "plan it"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_t1", "call_id": "call_t1", "type": "function",
            "function": {"name": "todo", "arguments": json.dumps(
                {"todos": [{"id": "1", "content": "检索经文",
                            "status": "completed"}]})},
        }]},
        {"role": "tool", "tool_call_id": "call_t1", "tool_name": "todo",
         "content": result},
    ]


def test_a_reloaded_todo_result_replays_the_checklist(monkeypatch):
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: _todo_msgs())})())
    events = hs.history("s", 0)
    todo = [e for e in events if e.get("kind") == "todo"]
    assert len(todo) == 1
    items = todo[0]["items"]
    assert [(t["id"], t["status"]) for t in items] == [
        ("1", "completed"), ("2", "in_progress"),
    ]
    assert items[0]["content"] == "检索经文"


def test_a_reloaded_todo_rows_show_summaries_not_the_json(monkeypatch):
    """Two rows survive a reload (the call and the result), and neither shows
    the raw list: the call row names what it was, the result row summarises."""
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: _todo_msgs())})())
    rows = [e for e in hs.history("s", 0) if e.get("kind") == "tool"]
    assert len(rows) == 2
    assert all("todos" not in r["detail"] for r in rows)
    assert rows[0]["detail"] == "任务清单"
    assert "2 项" in rows[1]["detail"] and "1 已完成" in rows[1]["detail"]


def test_a_non_todo_result_is_never_read_as_a_checklist(monkeypatch):
    """The tool NAME gates the parse: `terminal` legitimately returns JSON
    about todo files, and that is not a plan."""
    import json

    msgs = _msgs("grep todos .", "found", 0)
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: msgs)})())
    assert [e.get("kind") for e in hs.history("s", 0)] == [
        "history_user", "tool", "tool",
    ]


# ── the post-compression todo injection ────────────────────────────────────
#
# hermes re-injects the agent's active todo list as a USER message after
# context compression (`compressed.append({"role": "user", "content":
# todo_snapshot})`). It is an instruction to the MODEL, and rendered as
# speech it is a wall of `[>]` markers in a user bubble — verbatim from the
# report that found it.


def _injection_block():
    return (
        "[Your active task list was preserved across context compression]\n"
        "- [>] upload_images. 上传 6 张故事板图片到 ComfyUI (in_progress)\n"
        "- [>] generate_videos. 提交修复版 6 个 h3_i2v 视频生成任务 （fix BasicScheduler） (in_progress)\n"
        "- [ ] download_videos. 下载所有生成的视频 (pending)\n"
        "- [ ] deliver. 交付视频到 /app/static/ (pending)"
    )


def test_the_todo_injection_row_becomes_a_card_not_speech(monkeypatch):
    msgs = [{"role": "user", "content": _injection_block()}]
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: msgs)})())
    events = hs.history("s", 0)
    assert [e.get("kind") for e in events] == ["todo"]
    items = events[0]["items"]
    assert [(t["id"], t["status"]) for t in items] == [
        ("upload_images", "in_progress"),
        ("generate_videos", "in_progress"),
        ("download_videos", "pending"),
        ("deliver", "pending"),
    ]
    # Content with its own dots and brackets survives whole.
    assert items[1]["content"] == "提交修复版 6 个 h3_i2v 视频生成任务 （fix BasicScheduler）"


def test_a_real_user_message_is_never_read_as_an_injection(monkeypatch):
    msgs = [{"role": "user", "content": "什么是怨憎会苦"}]
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: msgs)})())
    assert [e.get("kind") for e in hs.history("s", 0)] == ["history_user"]


def test_an_injection_header_with_no_items_renders_nothing(monkeypatch):
    msgs = [{"role": "user",
             "content": "[Your active task list was preserved across context compression]"}]
    monkeypatch.setattr(hs, "_db", lambda: type("D", (), {
        "get_messages": staticmethod(lambda sid: msgs)})())
    assert hs.history("s", 0) == []
