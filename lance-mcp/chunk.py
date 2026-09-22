"""Markdown chunking, ported line for line from memory-mcp's docs.rs.

The port is deliberate rather than a better chunker: lance-mcp is measured
against memory-mcp on the same goldset, and a comparison that changed the
chunks along with the engine could not say which of the two moved the number.
Sizes are UTF-8 BYTES, as in Rust — a CJK character is three of them, so a
character count would make every chunk three times larger.

A chunk never crosses an ATX heading (fenced code masks headings); an oversized
section is split at paragraph boundaries, carrying the previous chunk's last
paragraph over when it is short enough.
"""
from dataclasses import dataclass

MAX_CHUNK_BYTES = 2800
MAX_OVERLAP_BYTES = 400


@dataclass
class Chunk:
    headings: list[str]
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    body: str


@dataclass
class _Para:
    start: int
    end: int
    text: str


def _blen(s: str) -> int:
    return len(s.encode())


def _heading_level(line: str):
    hashes = len(line) - len(line.lstrip("#"))
    if 1 <= hashes <= 6 and line[hashes:].startswith(" "):
        title = line[hashes + 1 :].strip().rstrip("#").strip()
        if title:
            return hashes, title
    return None


def _paragraphs(lines: list[str], first_line: int) -> list[_Para]:
    out: list[_Para] = []
    cur: list[tuple[int, str]] = []

    def flush():
        if not cur:
            return
        piece: list[tuple[int, str]] = []
        size = 0
        for n, l in cur:
            if size > 0 and size + _blen(l) + 1 > MAX_CHUNK_BYTES:
                out.append(_Para(piece[0][0], piece[-1][0], "\n".join(x for _, x in piece)))
                piece, size = [], 0
            size += _blen(l) + 1
            piece.append((n, l))
        if piece:
            out.append(_Para(piece[0][0], piece[-1][0], "\n".join(x for _, x in piece)))
        cur.clear()

    for i, l in enumerate(lines):
        if not l.strip():
            flush()
        else:
            cur.append((first_line + i, l))
    flush()
    return out


def chunk_markdown(text: str) -> list[Chunk]:
    # str.splitlines also breaks on \x0b, \x1c, U+2028…; Rust's lines() only on \n.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [l[:-1] if l.endswith("\r") else l for l in lines]

    sections: list[tuple[list[str], int, int, int]] = []
    stack: list[tuple[int, str]] = []
    in_fence = False
    sec_start = 0
    sec_path: list[str] = []
    for i, line in enumerate(lines):
        t = line.lstrip()
        if t.startswith("```") or t.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        h = _heading_level(line)
        if h:
            level, title = h
            if i > sec_start or sec_path:
                sections.append((list(sec_path), sec_start + 1, sec_start, i))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sec_path = [t for _, t in stack]
            sec_start = i
    if sec_start < len(lines) or sec_path:
        sections.append((list(sec_path), sec_start + 1, sec_start, len(lines)))

    chunks: list[Chunk] = []
    for path, first_line, lo, hi in sections:
        body_lines = lines[lo:hi]
        if all(not l.strip() for l in body_lines):
            continue
        paras = _paragraphs(body_lines, first_line)
        cur: list[_Para] = []
        size = 0
        overlap: list[str | None] = [None]

        def emit():
            if not cur:
                return
            body = ""
            if overlap[0] is not None:
                body = overlap[0] + "\n\n"
                overlap[0] = None
            body += "\n\n".join(p.text for p in cur)
            last = cur[-1]
            if _blen(last.text) <= MAX_OVERLAP_BYTES:
                overlap[0] = last.text
            chunks.append(Chunk(list(path), cur[0].start, last.end, body))
            cur.clear()

        for p in paras:
            if size > 0 and size + _blen(p.text) > MAX_CHUNK_BYTES:
                emit()
                size = _blen(overlap[0]) if overlap[0] is not None else 0
            size += _blen(p.text) + 2
            cur.append(p)
        emit()
    return chunks
