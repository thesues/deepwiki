"""Preserve reading order and citation headings when preparing mixed books."""
import zipfile

from prepare_books import epub_text, wrap_text
from ingest_docs import rows_for_text


def test_epub_uses_spine_and_preserves_headings(tmp_path):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        z.writestr("OEBPS/book.opf", '''<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <metadata><dc:title>测试书</dc:title><dc:creator>作者</dc:creator></metadata>
          <manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest>
          <spine><itemref idref="b"/><itemref idref="a"/></spine></package>''')
        z.writestr("OEBPS/a.xhtml", '<html><body><h1>后章</h1><p>后面的正文</p></body></html>')
        z.writestr("OEBPS/b.xhtml", '<html><head><title>不收录的标题</title></head><body><h1>先章</h1><p>道可道，<em>非常道</em>。</p><script>bad()</script></body></html>')
    text, metadata = epub_text(path)
    assert text.index("先章") < text.index("后章")
    assert "## 先章" in text and "道可道，非常道。" in text
    assert "bad()" not in text and "不收录的标题" not in text
    assert metadata["text_sections"] == 2


def test_long_prose_is_bounded_without_losing_text():
    original = "道可道，非常道。" * 400
    wrapped = wrap_text(original)
    assert "".join(wrapped.splitlines()) == original
    assert max(map(len, wrapped.splitlines())) <= 500
    rows, file = rows_for_text("# 书名\n\n## PDF 第12页\n\n" + wrapped, "tao/book.md", "book")
    assert len(rows) > 1
    assert all("PDF 第12页" in row["headings"] for row in rows if row["start"] >= 3)
    lines = file["text"].splitlines()
    for row in rows:
        assert row["text"].split("\n\n", 1)[1].strip() == "\n".join(lines[row["start"]-1:row["end"]]).strip()
