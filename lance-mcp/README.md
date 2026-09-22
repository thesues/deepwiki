# lance-mcp

Retrieval over unmodified community LanceDB tables on an Autumn FUSE mount,
measured against memory-mcp on memory-mcp's own goldset: document
ingestion with a full-text + hybrid eval, a code graph checked edge for edge
against memory-mcp's, and an MCP server exposing memory-mcp's tool surface
over the same wire protocol memory-mcp's HTTP transport uses.

The Python environment uses PyPI `lancedb==0.39.0`; there is no LanceDB fork,
custom provider, Python binding, or commit handler. In production the pod
mounts Autumn at `/mnt/autumn` and gives LanceDB the ordinary filesystem path
`/mnt/autumn/lancedb/buda`.

The image build uses ivolc Debian and PyPI as the primary indexes, with Aliyun
as an explicit fallback for packages or versions not yet mirrored by ivolc.
The base Python environment only bootstraps the `uv` executable; `uv sync
--frozen` installs the complete application dependency graph.

    uv sync --group dev
    PY="uv run --no-sync python"
    EVAL=~/upstream/autumn-rs/examples/memory-mcp/eval

    # index
    $PY ingest_docs.py --fs-root ~/Downloads/buda --index md --db-path /mnt/autumn/lancedb/buda \
        --embed-url http://llama-embed:8080 --embed-model bge-m3
    $PY ingest_code.py --fs-root ~/upstream --index autumn-rs --db-path /mnt/autumn/lancedb/buda \
        --embed-url http://llama-embed:8080 --embed-model bge-m3   # --embed-url optional

    # verify
    $PY test_chunk.py
    $PY test_server.py
    $PY eval_docs.py   --db-path /mnt/autumn/lancedb/buda --goldset $EVAL/sutra.jsonl \
        --modes lexical,vector,hybrid --embed-url http://llama-embed:8080
    $PY parity_code.py --db-path /mnt/autumn/lancedb/buda --mcp http://<a running memory-mcp>/mcp

    # serve
    $PY server.py --db-path /mnt/autumn/lancedb/buda --embed-url http://llama-embed:8080 --port 5102

`chunk.py` is a port of memory-mcp's `chunk_markdown`, not a new chunker: the
eval should compare engines, and new chunks would be a second variable.
`test_chunk.py` carries docs.rs's tests over; a change to docs.rs's chunker
has to be carried here by hand. Row ids, breadcrumbs and indexed text match
memory-mcp's, so `eval_docs.py` judges hits by eval.rs's rules and
`server.py`'s hits cite the same "file › headings, lines a-b".

Without `--embed-url` the `vector` column is left null on every row written
that ingest — `server.py` then reports that table as `lexical`-only rather
than serving vector/hybrid off nothing. A table's modes can differ: `docs`
may hold real vectors while `code` does not, and `GET /config` says which.

## Tokenizer, measured

21 files, 7433 chunks, 41 goldset queries, k=10, full-text only. The last row
is memory-mcp's lexical baseline (BM25 over CJK unigram+bigram):

| tokenizer        | hit@1 | hit@5 | MRR@10 | P@10  | FP@10 | found | lost                    |
|------------------|------:|------:|-------:|------:|------:|------:|-------------------------|
| ngram 1-2        | 0.951 | 1.000 | 0.976  | 0.690 | 0.002 | 41/41 | 授记 1→2                |
| ngram 1          | 0.927 | 0.976 | 0.946  | 0.624 | 0.002 | 41/41 | 慧能 2→9, 七处征心 1→3, 授记 1→3 |
| ngram 2          | 0.927 | 0.951 | 0.939  | 0.649 | 0.002 | 39/41 | 杏, 碗 missed           |
| icu              | 0.902 | 0.976 | 0.935  | 0.661 | 0.000 | 40/41 | 杏 missed, 碗 1→3        |
| memory-mcp       | 0.976 | 1.000 | 0.988  | 0.712 | 0.002 | 41/41 |                         |

ngram 1-2 is the default (`--tokenizer` selects the others). Unigrams alone
bury 慧能 under 智慧能; bigrams alone cannot match a one-character query;
ICU segments 杏 into longer words. The one rank ngram 1-2 loses, 授记, is a
near-tie (17.09 vs 16.99) with a 金光明经 passage that says 授记 six times —
a file-level label calls it wrong, a reader would not.

The two indexes emit the same terms but are not the same BM25: autumn-memory
leaves bigrams out of `doc_len` (recall.rs), Lance's ngram tokenizer counts
every token, so long CJK chunks are normalised about twice as hard here. That
is the likeliest source of the remaining gap; it is not measured.

The corpus directory holds 21 files while the goldset's header describes 17,
so the P@10 gap may be partly corpus rather than engine; memory-mcp's baseline
does not record which files it indexed.

jieba needs its model under `LANCE_LANGUAGE_MODEL_HOME` and is not measured yet.

## Hybrid, measured

Same 41 queries, k=10, real BGE-M3 vectors (llama-embed, 1024-d) over the same
7433 chunks. `hybrid` fuses lexical + vector by autumn-memory's own rule:
each leg to depth `max(k,10)*2`, reciprocal-rank sum `1/(60+rank)`, ties
broken by id — `server.py`'s `Retriever.search` and `eval_docs.py` both go
through it, so this is what a hermes profile actually gets from `mode=auto`.

| mode    | hit@1 | hit@5 | hit@k | MRR@10 | P@10  | found |
|---------|------:|------:|------:|-------:|------:|------:|
| lexical | 0.951 | 1.000 | 1.000 | 0.976  | 0.690 | 41/41 |
| vector  | 0.829 | 0.927 | 0.951 | 0.873  | 0.595 | 39/41 |
| hybrid  | 0.902 | 1.000 | 1.000 | 0.947  | 0.688 | 41/41 |

Vector alone misses 时时勤拂拭 and 蒲团 — short, quotable phrases an
embedding doesn't privilege the way exact-match BM25 does; hybrid's lexical
leg recovers both, back to 41/41 found. Vector's hit@1 costs hybrid's: fusing
in a leg that ranks some right answers lower moves hit@1 from lexical's 0.951
to 0.902, the predictable trade of adding a second, noisier signal.

Head-to-head against memory-mcp, same corpus, same llama-embed, same
goldset (its docs.rs embeds one chunk per sequential unbatched round trip —
by design, see its pass-1/pass-2 split comment — so this run took ~2h against
lance's ingest at a few minutes):

| mode    | hit@1 | hit@5 | hit@k | MRR@10 | P@10  | found | engine     |
|---------|------:|------:|------:|-------:|------:|------:|------------|
| lexical | 0.951 | 1.000 | 1.000 | 0.976  | 0.690 | 41/41 | lance      |
| lexical | 0.951 | 0.976 | 0.976 | 0.963  | 0.663 | 40/41 | memory-mcp |
| vector  | 0.829 | 0.927 | 0.951 | 0.873  | 0.595 | 39/41 | lance      |
| vector  | 0.610 | 0.756 | 0.756 | 0.675  | 0.515 | 31/41 | memory-mcp |
| hybrid  | 0.902 | 1.000 | 1.000 | 0.947  | 0.688 | 41/41 | lance      |
| hybrid  | 0.756 | 0.902 | 0.951 | 0.804  | 0.646 | 39/41 | memory-mcp |

Lance's vector leg is well ahead of memory-mcp's on the same embedder and
corpus (hit@1 0.829 vs 0.610), and that carries into hybrid (0.902 vs
0.756). The two use different vector indexes — Lance's IVF over this
fork's LanceDB, autumn-memory's own IVF (`NPROBE=8`, recall.rs) — and this
measurement does not say which design choice explains the gap, only that it
exists. Lexical is close (lance edges it: 41/41 vs 40/41 found), consistent
with the ngram-tokenizer section above. memory-mcp's own single-character
misses (杏, 碗 — one-character queries) and 蒲团 (a 2-char, count-4 term) are
exactly the cases that section's tokenizer sweep predicts.

## Embedding, and a production bug found measuring it

BGE-M3 embedding on the corpus was catastrophically slow before this work —
~0.36 embeds/s from a shared `llama-embed`, an 11-hour ETA for both this
corpus's ingest and a comparison memory-mcp run. Root cause, found by pausing
both clients (`SIGSTOP`, not kill) and sampling `llama-embed`'s own `/slots`
and log under a clean, controlled load: `k8s/llama-embed.yaml` ran
`--ubatch-size 8192`, equal to `--ctx-size`. `--ubatch-size` is the *physical*
batch one forward pass computes; pinning it to the context size means every
call pads to a full 8192-token pass regardless of the actual input — a ~930
token chunk was paying for a tensor nine times its size. `--batch-size` (the
*logical* batch, which does need to cover the longest single sequence) was
already correctly sized; only `--ubatch-size` was the mistake.

Fix (rolled out, live): `--ubatch-size 2048` — safe for this corpus's chunks
(memory-mcp's own comment on the manifest says they run to ~930 tokens) with
margin, far under 8192. Confirmed with the server's own log under real
concurrent load, two separate 15-second windows: **~8.5 embeds/s, ~24x**.
Isolated single-request latency barely moved (1.16s → ~1.7-2.1s) — the
padding cost was specifically a *concurrency* tax, not a per-request one,
consistent with 4 slots each forcing their own full-size physical batch
rather than packing into one.

**Known gap left by this fix**: 2048 tokens is not enough for the largest
Rust symbols — `ingest_code.py` found one impl block whose raw text is
191,387 characters, clipped to ~7000 tokens by `embed.py`'s `clip()` (ported
from autumn-memory's `embed.rs`, same 7000-token budget). A symbol that size
now 500s on embedding and falls back to lexical-only indexing — both this
indexer and memory-mcp's already treat that as a per-chunk degradation, not a
failure (see `n_novec` in indexer.rs and the equivalent path here), so
nothing breaks, but code-index-mcp's vector leg is quietly missing its
largest symbols. Not fixed here: either raise `--ubatch-size` further (cost:
the same padding tax returns) or cap what an indexer will *submit* to
embedding — chunk large code bodies the way `docs.rs` already chunks prose,
which is probably the right fix and wasn't in scope for this measurement.

## Code graph

`ingest_code.py` ports memory-mcp's indexer.rs into two tables:
`code` (one per symbol, id `<relpath>::<qualname>`, source as text,
full-text on the simple tokenizer), and `edges` (`src`, `type`, `dst`, a
btree on each end). CALLS resolve by short name, capped at 8 targets per name,
as memory-mcp's do — including its over-linking, e.g. a test calling its own
`block_on` is also linked to a `block_on` in another crate. The cap makes file
order part of the answer, so the walk uses readdir order, not sorted order.

`find_callers(id)` / `find_callees(id)` become
`edges WHERE type = 'CALLS' AND dst|src = id`. `parity_code.py` checks that
against a memory-mcp that indexed the same tree:

| autumn-rs, 279 files | memory-mcp | lance |
|---|---:|---:|
| symbols            | 6121 defs | 6119 ids (2 duplicate ids fold, last wins as in its store) |
| edges              | 74127     | 74127 (66421 CALLS, 7706 CONTAINS) |
| callers + callees, every symbol | 12238 queries | 0 differ |

The zero is checked, not assumed: the reference returned 66421 caller edges
in total, and deleting one CALLS row from lance makes the script report
exactly two differences and exit 1. Parse 1–3 s, write 0.4 s, indexes 1 s.
The grammar is tree-sitter-rust 0.23.3, as memory-mcp's Cargo.lock; the
Python runtime is 0.25 only because that grammar's wheel is ABI 15.

## Server

`server.py` is a drop-in for the tools a hermes profile names — same names,
arguments, result shapes and defaults (`k=8`, `mode=auto` → `hybrid` when a
table has vectors) as memory-mcp's, over the same JSON-RPC 2.0 wire shape at
`POST /mcp` (batch or single, a notification gets 202 with no body, an
unknown method -32601). A profile's `mcp_servers`/`mcp_tools` can point at
this instead of memory-mcp with no other change. `graph_upsert_node` and the
other generic graph_* tools are not carried over — nothing in buda's profiles
calls them.

What's different is where answers come from: searches are Lance queries
(full-text and/or vector, fused by the rule above), `find_callers`/
`find_callees`/`trace_call_path`/`document_outline` are filters and BFS over
the `edges` table (one query per BFS level, not one per node), and
`read_file` reads the `files` table rather than reopening corpus files. The
container is privileged only because it owns its Autumn FUSE mount; the Python
code and community LanceDB see a normal local directory and contain no Autumn
connection API.
`ingest_documents` (the one write tool) re-chunks and re-embeds a path under
`--fs-root` and replaces just the rows under it, so an incremental re-index
doesn't leave orphaned chunks at moved line spans beside their replacement.

`test_server.py` exercises every tool — all four search modes, `get_symbol`,
`read_file`'s bounds (including the inverted-range and past-the-end cases
that once crashed memory-mcp's server, per store.rs's own comment),
`find_callers`/`find_callees`, `trace_call_path`, `document_outline`, and the
MCP envelope itself (`tools/list`, an unknown method, a notification getting
no reply, a tool error) — against an in-memory Lance database with a fake
deterministic embedder, so it needs no cluster and no embedding server.

**Verified against the real corpus** (7433 sutra chunks, real BGE-M3
vectors, 6119 autumn-rs symbols, lexical-only): `search_docs` in `hybrid`
mode correctly retrieves 坐正、背挺直、手放好 for "坐禅的时候应该保持什么
姿势"; `document_outline` walks the file → section tree in the right depth
order; `read_file` returns the exact cited lines; `search_code` finds
`add_edge` by name; `search_code(mode=vector)` answers the "no embedder for
this table" tool error rather than 500ing, since `code` currently holds no
vectors (see below); `find_callers` matches `parity_code.py`'s own count.
This is the same tool surface, arguments and server the `buda` profile in
`k8s/webui.yaml` is scoped to (`mcp_tools: search_docs, list_documents,
document_outline, read_file`) — checked by calling `POST /mcp` directly with
those exact tool names and arguments, not by running the webui itself: no
hermes venv exists on this machine to drive a real chat turn through
FreeToken. `k8s/lance-mcp.yaml` supplies the Service and `k8s/webui.yaml`
grants its four read-only tools to that profile.

## Timing

The measurements below were taken with the retired native-provider prototype;
the query/index behavior is LanceDB's, but FUSE adds a different storage path
and must be measured independently after rollout. Ingest on that local 3-node
cluster: chunking 0.1 s, write 0.4 s, FTS build
2–8 s depending on tokenizer. Two writes out of about ten took 30–36 s
instead of 0.4 s. The second coincided with the partition server's append to
extent node 9102 timing out (5.5 s), the stream being poisoned, and the
manager answering "no healthy node available to allocate extent 14" while
node2 itself logged sub-millisecond appends; the first left no such warning.
Autumn-side, and not investigated further here.
