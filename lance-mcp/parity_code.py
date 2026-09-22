"""Check lance's code graph against a running memory-mcp that indexed the same tree.

    memory-mcp 127.0.0.1:9001 --fs-root ~/upstream --index autumn-rs --code \
        --agent lance-parity --reset --port 5190          # the reference
    python ingest_code.py --fs-root ~/upstream --index autumn-rs \
        --db-path /mnt/autumn/lancedb/buda
    python parity_code.py --db-path /mnt/autumn/lancedb/buda \
        --mcp http://127.0.0.1:5190/mcp

For every symbol in the lance `code` table, find_callers and find_callees
are asked of memory-mcp (a graph walk) and of lance (an equality filter on
`edges`), and the id sets compared. Exit 1 on any difference.
"""
import argparse
import concurrent.futures
import json
import sys
import urllib.request
from pathlib import Path

from store import connect


def mcp_call(url: str, tool: str, sid: str) -> set[str]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": {"id": sid}}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    if "error" in resp:
        raise RuntimeError(f"{tool}({sid}): {resp['error']}")
    items = json.loads(resp["result"]["content"][0]["text"])
    return {i["id"] for i in items}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", required=True, type=Path)
    ap.add_argument("--mcp", required=True, help="memory-mcp's POST /mcp url")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    db = connect(args.db_path)
    ids = [r["id"] for r in db.open_table("code").search().select(["id"]).limit(None).to_list()]
    calls = db.open_table("edges").search().where("type = 'CALLS'") \
        .select(["src", "dst"]).limit(None).to_arrow().to_pylist()
    # One read of the edge table, then set lookups: the per-id filter is what
    # a server would issue, but 12k round trips would time the network here,
    # not check the answers.
    callers: dict[str, set[str]] = {}
    callees: dict[str, set[str]] = {}
    for e in calls:
        callers.setdefault(e["dst"], set()).add(e["src"])
        callees.setdefault(e["src"], set()).add(e["dst"])
    # ...and a handful through the filter itself, so the SQL path is exercised.
    et = db.open_table("edges")
    for sid in ids[:20]:
        got = {r["src"] for r in et.search().where(f"type = 'CALLS' AND dst = '{sid}'")
               .select(["src"]).limit(None).to_list()}
        assert got == callers.get(sid, set()), sid

    def check(sid: str):
        return (sid, mcp_call(args.mcp, "find_callers", sid), mcp_call(args.mcp, "find_callees", sid))

    diffs = ref_edges = 0
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for sid, ref_in, ref_out in pool.map(check, ids):
            ref_edges += len(ref_in)
            for what, ref, got in (("callers", ref_in, callers.get(sid, set())),
                                   ("callees", ref_out, callees.get(sid, set()))):
                if ref != got:
                    diffs += 1
                    if diffs <= 20:
                        print(f"DIFF {what} {sid}\n  only memory-mcp: {sorted(ref - got)[:5]}"
                              f"\n  only lance:      {sorted(got - ref)[:5]}")
    n_in = sum(len(v) for v in callers.values())
    # A zero diff only means something if memory-mcp answered: an empty or
    # truncated reference would compare equal to nothing at all.
    print(f"{len(ids)} symbols, {n_in} CALLS edges in lance, {ref_edges} from memory-mcp; "
          f"{2 * len(ids)} queries compared, {diffs} differ")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
