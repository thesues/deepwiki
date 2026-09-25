"""Tests for the PDF-corpus tools: read_page aggregation, page_images as MCP
image content, and the watermark rule of the ingest.

    python -m pytest test_pages.py -q   (or: python test_pages.py)

The store is a local-memory LanceDB (same shape as test_server.py's), the
images are in-memory PNGs — no cluster, no poppler.
"""
import base64
import struct
import tempfile
from pathlib import Path

import pyarrow as pa
import pytest

from ingest_pdfs import PAGE_IMAGES_SCHEMA, _is_content
from server import Content, Retriever, ToolError, call_tool, tools_for
from store import connect


def tiny_png(rgb: bytes, w: int = 4, h: int = 4) -> bytes:
    """A minimal valid truecolor PNG — big enough for png_size, small enough
    to not care about its pixels."""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + rgb * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


DOC = "mayi/test.pdf"


def rows_for_pages(pages: dict[int, list[str]]) -> list[dict]:
    out = []
    for pg in sorted(pages):
        for i, body in enumerate(pages[pg]):
            out.append({"id": f"{DOC}#p{pg}-{i}", "file": DOC,
                        "name": f"第{pg}页", "headings": [f"第{pg}页"],
                        "start": pg, "end": pg, "parent": DOC,
                        "text": f"{DOC} › 第{pg}页\n\n{body}", "vector": None})
    return out


@pytest.fixture()
def r(tmp_path: Path) -> Retriever:
    db = connect(str(tmp_path / "db"))
    pages = {3: ["面大而黑，主贵。", "额方者贵。"],
             4: ["山根平满者，主寿。"],
             12: ["印堂开阔，心胸亦宽。"]}
    docs = pa.Table.from_pylist(rows_for_pages(pages),
                                schema=__import__("ingest_docs").SCHEMA)
    db.create_table("docs", docs, mode="overwrite", on_bad_vectors="null")
    imgs = pa.Table.from_pylist([
        {"file": DOC, "page": 3, "kind": "figure", "width": 4, "height": 4,
         "sha": "a", "image": tiny_png(b"\xff\x00\x00")},
        {"file": DOC, "page": 4, "kind": "figure", "width": 8, "height": 8,
         "sha": "b", "image": tiny_png(b"\x00\xff\x00", 8, 8)},
    ], schema=PAGE_IMAGES_SCHEMA)
    db.create_table("page_images", imgs, mode="overwrite")
    return Retriever(db, None)


def test_read_page_groups_and_orders(r: Retriever):
    out = call_tool(r, "read_page", {"file": DOC, "page_start": 3})
    assert out["isError"] is False if "isError" in out else True
    text = out["content"][0]["text"]
    assert "第3页" in text and "面大而黑" in text and "额方者贵" in text
    assert "第4页" not in text and "山根" not in text
    # two chunks of the same page come back as one page, breadcrumb stripped
    assert text.count(f"{DOC} ›") == 1  # only the separator carries it


def test_read_page_range_and_errors(r: Retriever):
    out = call_tool(r, "read_page", {"file": DOC, "page_start": 3, "page_end": 4})
    t = out["content"][0]["text"]
    assert "山根平满" in t and "印堂" not in t
    err = call_tool(r, "read_page", {"file": DOC, "page_start": 99})
    assert err.get("isError") is True and "no indexed pages" in err["content"][0]["text"]
    err = call_tool(r, "read_page", {"file": DOC, "page_start": 5, "page_end": 2})
    assert err.get("isError") is True


def test_page_images_are_mcp_image_content(r: Retriever):
    out = call_tool(r, "page_images", {"file": DOC, "page_start": 3, "page_end": 12})
    kinds = [c["type"] for c in out["content"]]
    assert kinds[0] == "text" and kinds.count("image") == 2
    meta = __import__("json").loads(out["content"][0]["text"])
    assert meta["count"] == 2 and meta["images"][0]["page"] == 3
    img = next(c for c in out["content"] if c["type"] == "image")
    assert img["mimeType"] == "image/png"
    assert base64.b64decode(img["data"])[:4] == b"\x89PNG"


def test_page_images_empty_is_a_readable_error(r: Retriever):
    out = call_tool(r, "page_images", {"file": DOC, "page_start": 90})
    assert out.get("isError") is True and "no figures" in out["content"][0]["text"]


def test_tools_for_needs_the_table(r: Retriever, tmp_path: Path):
    assert [t["name"] for t in tools_for(r)].count("page_images") == 1
    bare = Retriever(connect(str(tmp_path / "empty")), None)
    assert all(t["name"] not in ("page_images", "read_page") for t in tools_for(bare))


def test_watermark_rule():
    # plan-b's numbers: watermark on every page, figures at most twice.
    npages = 135
    assert not _is_content(freq=134, npages=npages)
    assert _is_content(freq=2, npages=npages)
    # a small doc: any identical image on 4+ pages is decoration, however few pages
    assert not _is_content(freq=4, npages=6)
    assert _is_content(freq=3, npages=6)


if __name__ == "__main__":
    raise SystemExit("run with pytest")
