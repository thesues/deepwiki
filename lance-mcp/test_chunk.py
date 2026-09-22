"""docs.rs's chunker tests, ported: the port is only useful while it agrees.

    python -m pytest test_chunk.py      (or: python test_chunk.py)
"""
from chunk import chunk_markdown


def test_heading_hierarchy_and_line_spans():
    md = "intro line\n\n# A\n\ntext a\n\n## B\n\ntext b\n\n# C\n\ntext c\n"
    chunks = chunk_markdown(md)
    assert [(c.headings, c.start_line, c.end_line) for c in chunks] == [
        ([], 1, 1), (["A"], 3, 5), (["A", "B"], 7, 9), (["C"], 11, 13),
    ]
    assert "text b" in chunks[2].body


def test_fenced_hash_is_not_a_heading():
    chunks = chunk_markdown("# Real\n\n```\n# not a heading\n```\nafter\n")
    assert len(chunks) == 1
    assert chunks[0].headings == ["Real"]
    assert "# not a heading" in chunks[0].body


def test_oversized_section_splits_at_paragraphs_with_overlap():
    para = "x" * 1500
    chunks = chunk_markdown(f"# Big\n\n{para}\n\nSHORT TAIL\n\n{para}\n")
    assert len(chunks) >= 2
    assert all(c.headings == ["Big"] for c in chunks)
    assert sum("SHORT TAIL" in c.body for c in chunks) >= 2, "overlap not carried"
    spans = [(c.start_line, c.end_line) for c in chunks]
    assert len(set(spans)) == len(spans)


def test_plain_text_without_headings_chunks_by_paragraphs():
    chunks = chunk_markdown(f"{'a' * 1500}\n\n{'b' * 1500}\n\n{'c' * 200}")
    assert len(chunks) >= 2
    assert all(not c.headings for c in chunks)


def test_the_cap_counts_utf8_bytes_not_characters():
    # Two 500-character paragraphs: 1000 chars fit the cap, 3000 bytes do not.
    han = "佛" * 500
    chunks = chunk_markdown(f"# 经\n\n{han}\n\n{han}\n")
    assert len(chunks) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
