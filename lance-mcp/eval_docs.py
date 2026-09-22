"""Score lance-mcp's search_docs against memory-mcp's goldset.

    EVAL=~/upstream/autumn-rs/examples/memory-mcp/eval
    python eval_docs.py --db-path /mnt/autumn/lancedb/buda \
        --goldset $EVAL/sutra.jsonl --baseline $EVAL/baseline.json

The goldset and baseline are memory-mcp's and stay in autumn-rs beside it:
this script reads them, it does not own them.

Judgment and metrics are eval.rs's, so the numbers sit next to memory-mcp's
lexical baseline: a hit is relevant if its file ends with an expect_file, its
text contains an expect_substr or its id is an expect_id; a reject_substr in
the text overrides all of those. P@k and FP@k divide by k, not by the hits
returned, so a short page is not rewarded for being short.
"""
import argparse
import json
from pathlib import Path

from embed import Embedder
from server import Retriever
from store import connect

def load(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        t = line.strip()
        if t and not t.startswith("#"):
            out.append(json.loads(t))
    return out


def judge(q: dict, hit: dict) -> str:
    text, file, rid = hit["text"], hit["file"], hit["id"]
    if any(s in text for s in q.get("reject_substr", [])):
        return "rejected"
    if (any(file.endswith(f) for f in q.get("expect_file", []))
            or any(s in text for s in q.get("expect_substr", []))
            or rid in q.get("expect_id", [])):
        return "relevant"
    return "irrelevant"


def evaluate(r: Retriever, mode: str, queries: list[dict], k: int) -> dict:
    """Through the server's own search, so what is scored is what it serves.
    Hits carry no body (a search hit is a location), so the text the labels
    are judged against is fetched by id afterwards, as memory-mcp's eval does."""
    ranks, outcomes, misses = {}, [], []
    texts = r.db.open_table(r.docs_table)
    for q in queries:
        hits = r.search("docs", q["q"], mode, k)
        if hits:
            ids = ", ".join("'" + h["id"].replace("'", "''") + "'" for h in hits)
            body = {row["id"]: row["text"] for row in texts.search().where(f"id IN ({ids})")
                    .select(["id", "text"]).limit(None).to_list()}
            for h in hits:
                h["text"] = body[h["id"]]
        rank = n_rel = n_rej = 0
        for i, h in enumerate(hits):
            j = judge(q, h)
            if j == "relevant":
                n_rel += 1
                rank = rank or i + 1
            elif j == "rejected":
                n_rej += 1
        ranks[q["q"]] = rank
        outcomes.append((rank, n_rel, n_rej))
        if not rank:
            misses.append((q["q"], [f'{h["id"]} [{h["score"]:.3f}]' for h in hits[:3]]))
    n = len(outcomes)
    within = lambda lim: sum(1 for r, _, _ in outcomes if 0 < r <= lim) / n
    r4 = lambda x: round(x, 4)
    metrics = {
        "hit@1": r4(within(1)),
        "hit@5": r4(within(min(5, k))),
        "hit@k": r4(within(k)),
        "mrr@k": r4(sum(1 / r for r, _, _ in outcomes if r) / n),
        "p@k": r4(sum(nr / k for _, nr, _ in outcomes) / n),
        "fp@k": r4(sum(nj / k for _, _, nj in outcomes) / n),
    }
    return {"metrics": metrics, "ranks": ranks, "misses": misses}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", required=True, type=Path)
    ap.add_argument("--table", default="docs")
    ap.add_argument("--goldset", type=Path, required=True, help="memory-mcp eval/sutra.jsonl")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--modes", default="lexical", help="comma list of lexical,vector,hybrid")
    ap.add_argument("--embed-url")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--baseline", type=Path, help="a memory-mcp eval report to set beside, "
                    "compared mode for mode")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    db = connect(args.db_path)
    emb = Embedder(args.embed_url, args.embed_model) if args.embed_url else None
    r = Retriever(db, emb, docs_table=args.table)
    queries = load(args.goldset)
    base = json.loads(args.baseline.read_text())["modes"] if args.baseline else {}
    report = {"k": args.k, "queries": len(queries), "modes": {}}
    for mode in args.modes.split(","):
        res = evaluate(r, mode, queries, args.k)
        report["modes"][mode] = {k: res[k] for k in ("metrics", "ranks")}
        m = res["metrics"]
        found = sum(1 for x in res["ranks"].values() if x)
        print(f"\nmode={mode}")
        print("  lance      " + "  ".join(f"{k} {v:.3f}" for k, v in m.items())
              + f"   ({found}/{len(queries)} found)")
        b = base.get(mode)
        if b:
            print("  memory-mcp " + "  ".join(f"{k} {b['metrics'][k]:.3f}" for k in m))
            moved = [(q, b["ranks"].get(q, 0), x) for q, x in res["ranks"].items()
                     if x != b["ranks"].get(q, 0)]
            for q, was, now in sorted(moved, key=lambda t: (t[2] == 0, -(t[2] or 99))):
                print(f"    {q:<20} {was} → {now}   (memory-mcp → lance; 0 = not in top k)")
        for q, top in res["misses"]:
            print(f"  MISS {q}: {', '.join(top)}")
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
