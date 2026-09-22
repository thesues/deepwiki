"""Embeddings from a server speaking OpenAI's /v1/embeddings (llama-embed).

The limits are autumn-memory's embed.rs, carried over because they were set
against this same server: BGE-M3 has an 8192-token context and llama.cpp a
physical batch of the same size, and one input or one request over it is a
500 that ends an ingest. There is no tokenizer here either, so tokens are
estimated — ASCII at a third of one, anything else at a whole one, because
CJK runs about a token per character and a code-shaped guess would silently
clip the Chinese corpus.
"""
import json
import time
import urllib.request

import numpy as np

EMBED_TOKEN_BUDGET = 7000
BATCH_TOKEN_BUDGET = 7000
BATCH_MAX_INPUTS = 64
ATTEMPTS = 3


def est_tokens(text: str) -> float:
    ascii_n = sum(1 for c in text if c.isascii())
    return ascii_n / 3 + (len(text) - ascii_n)


def clip(text: str) -> str:
    est = 0.0
    for i, c in enumerate(text):
        est += 1 / 3 if c.isascii() else 1.0
        if est > EMBED_TOKEN_BUDGET:
            return text[:i]
    return text


def batches(texts: list[str]) -> list[range]:
    out, start, tokens = [], 0, 0.0
    for i, t in enumerate(texts):
        n = est_tokens(t)
        if i - start >= BATCH_MAX_INPUTS or (i > start and tokens + n > BATCH_TOKEN_BUDGET):
            out.append(range(start, i))
            start, tokens = i, 0.0
        tokens += n
    if start < len(texts):
        out.append(range(start, len(texts)))
    return out


def embeddings_url(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/embeddings"):
        return b
    return f"{b}/embeddings" if b.endswith("/v1") else f"{b}/v1/embeddings"


class Embedder:
    def __init__(self, url: str, model: str, timeout: float = 120.0):
        self.url = embeddings_url(url)
        self.model = model
        self.timeout = timeout
        self.dim = 0

    def embed(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        clipped = [clip(t) for t in texts]
        out: list[np.ndarray] = []
        for r in batches(clipped):
            out.extend(self._with_retry(clipped[r.start:r.stop]))
        return out

    def _with_retry(self, texts: list[str]) -> list[np.ndarray]:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(0.25 * (2 ** attempt))
            try:
                return self._once(texts)
            except Exception as e:  # noqa: BLE001 — the last one is re-raised
                last = e
        raise RuntimeError(f"embeddings call to {self.url} failed {ATTEMPTS}x: {last}")

    def _once(self, texts: list[str]) -> list[np.ndarray]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.load(r)["data"]
        if len(data) != len(texts):
            # Vectors are matched to inputs by position; a short reply would
            # put every later vector on the wrong chunk.
            raise RuntimeError(f"asked for {len(texts)} vectors, got {len(data)}")
        vecs = [None] * len(texts)
        # A server that batches internally may answer out of order; `index` is
        # the only thing that says which input a vector belongs to.
        for d in data:
            v = np.asarray(d["embedding"], dtype=np.float32)
            n = np.linalg.norm(v)
            vecs[d["index"]] = v / n if n else v
        self.dim = len(vecs[0])
        return vecs
