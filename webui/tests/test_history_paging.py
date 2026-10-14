import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
import hermes_session_api as hs
from history_paging import history_page


@pytest.fixture
def database(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, active INTEGER DEFAULT 1,
            tool_calls TEXT, tool_call_id TEXT, tool_name TEXT);
        CREATE INDEX messages_session ON messages(session_id);
        INSERT INTO sessions VALUES ('root', NULL), ('tip', 'root'), ('other', NULL);
    """)
    db = SimpleNamespace(_conn=conn, _lock=threading.Lock())
    monkeypatch.setattr(hs, "_db", lambda: db)
    yield conn
    conn.close()


def add(conn, sid, text, role="user", **kw):
    columns = ["session_id", "role", "content", *kw]
    conn.execute(f"INSERT INTO messages ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 [sid, role, text, *kw.values()])


def texts(page):
    return [e["text"] for m in page["messages"] for e in m["events"] if e["kind"] == "history_user"]


def test_bidirectional_keyset_across_compression_with_concurrent_append(database):
    for i in range(125):
        add(database, "root" if i < 70 else "tip", str(i))
    add(database, "other", "private")
    add(database, "tip", "rewound", active=0)
    tail = history_page("tip")
    assert texts(tail) == list(map(str, range(85, 125)))
    assert tail["hasMoreBefore"] and not tail["hasMoreAfter"]
    add(database, "tip", "125")
    middle = history_page("tip", before=tail["before"])
    oldest = history_page("tip", before=middle["before"], limit=80)
    assert texts(oldest) + texts(middle) + texts(tail) == list(map(str, range(125)))
    assert not oldest["hasMoreBefore"]
    forward = history_page("tip", after=middle["after"])
    assert texts(forward) == texts(tail) and forward["hasMoreAfter"]
    assert texts(history_page("tip", after=forward["after"])) == ["125"]
    # Rewinding/deleting the boundary row cannot shift all subsequent pages.
    database.execute("DELETE FROM messages WHERE content = '85'")
    assert texts(history_page("tip", before=tail["before"])) == texts(middle)


def test_page_split_between_call_and_result_keeps_invocation(database):
    add(database, "tip", "", "assistant", tool_calls=json.dumps([
        {"id": "call-1", "function": {"name": "terminal", "arguments": '{"command":"echo hello"}'}}
    ]))
    add(database, "tip", "hello", "tool", tool_call_id="call-1", tool_name="terminal")
    page = history_page("tip", limit=1)
    event = page["messages"][0]["events"][0]
    assert event["status"] == "completed"
    assert "echo hello" in event["detail"] and "hello" in event["detail"]


def test_empty_and_hidden_rows_still_advance_cursor(database):
    assert history_page("tip")["messages"] == []
    add(database, "tip", "visible")
    add(database, "tip", "internal", "system")
    page = history_page("tip", limit=1)
    assert page["messages"][0]["events"] == [] and page["hasMoreBefore"]
    assert texts(history_page("tip", before=page["before"])) == ["visible"]


def test_cursor_is_scoped_and_input_is_bounded(database):
    add(database, "tip", "hello")
    cursor = history_page("tip")["before"]
    for kwargs in [{"before": cursor, "after": cursor}, {"before": "garbage"}, {"limit": 0}, {"limit": 81}]:
        with pytest.raises(ValueError):
            history_page("tip", **kwargs)
    with pytest.raises(ValueError):
        history_page("other", before=cursor)


def test_reads_are_limited_in_sql(database):
    for i in range(500):
        add(database, "tip", str(i))
    statements = []
    database.set_trace_callback(statements.append)
    page = history_page("tip")
    assert len(page["messages"]) == 40
    selects = [q for q in statements if q.startswith("SELECT * FROM messages")]
    assert selects and all(" LIMIT " in q and "OFFSET" not in q for q in selects)
