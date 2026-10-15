"""Convert EPUB/PDF books to citation-preserving Markdown for ingest_docs.

PDFs retain physical page headings. Image-only pages use the existing macOS
Vision OCR helper. A hash-keyed page cache makes long scans resumable; source
books are read-only. EPUB chapters follow OPF spine order, not ZIP order.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile


class ChapterText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body = False
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.body = True
        if not self.body:
            return
        if tag in {"script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * min(int(tag[1]) + 1, 6) + " ")
        elif tag in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag == "body":
            self.body = False
        if tag in {"p", "div", "li", "tr", "blockquote"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.body and not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def wrap_text(text: str) -> str:
    """Bound prose line lengths so the existing byte chunker can split them."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        while len(line) > 500 and not line.startswith("#"):
            boundaries = list(re.finditer(r"[。！？；]", line[:500]))
            cut = boundaries[-1].end() if boundaries else 500
            lines.append(line[:cut])
            line = line[cut:]
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def epub_text(path: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf = next(x.attrib["full-path"] for x in container.iter() if x.tag.endswith("}rootfile"))
        package = ET.fromstring(archive.read(opf))
        titles = [x.text for x in package.iter() if x.tag.endswith("}title") and x.text]
        title = (titles[0] if titles else path.stem).split("【", 1)[0]
        authors = [x.text for x in package.iter() if x.tag.endswith("}creator") and x.text]
        items = {x.attrib["id"]: x.attrib for x in package.iter() if x.tag.endswith("}item")}
        spine = [x.attrib["idref"] for x in package.iter() if x.tag.endswith("}itemref")]
        sections = [f"# {title}", "作者：" + "、".join(authors)]
        included = []
        for item_id in spine:
            item = items[item_id]
            member = posixpath.normpath(posixpath.join(posixpath.dirname(opf),
                       urllib.parse.unquote(item["href"].split("#", 1)[0])))
            parser = ChapterText()
            parser.feed(archive.read(member).decode("utf-8-sig"))
            text = wrap_text("".join(parser.parts)).strip()
            if text:
                sections.append(text)
                included.append(member)
        if not included:
            raise ValueError(f"EPUB has no readable spine content: {path}")
        return "\n\n".join(sections) + "\n", {"title": title, "authors": authors,
                "spine_entries": len(spine), "text_sections": len(included)}


def ocr_page(args):
    pdf, page, cache = args
    cached = Path(cache) / f"{page:04d}.json"
    if cached.exists():
        return page, json.loads(cached.read_text())["text"]
    from ocr import ocr_png
    with tempfile.TemporaryDirectory(prefix="book-ocr-") as directory:
        prefix = str(Path(directory) / "page")
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                        "-scale-to", "2400", "-png", pdf, prefix], check=True, capture_output=True)
        text = ocr_png(Path(prefix + ".png").read_bytes())
    cached.parent.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_suffix(".tmp")
    temporary.write_text(json.dumps({"page": page, "text": text}, ensure_ascii=False))
    temporary.replace(cached)
    return page, text


def pdf_text(path: Path, cache: Path, workers: int) -> tuple[str, dict]:
    info = subprocess.run(["pdfinfo", str(path)], check=True, capture_output=True, text=True).stdout
    pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
    raw = subprocess.run(["pdftotext", str(path), "-"], check=True, capture_output=True, text=True).stdout
    texts = raw.split("\f")[:pages]
    texts += [""] * (pages - len(texts))
    missing = [i + 1 for i, text in enumerate(texts) if len(text.strip()) < 20]
    if missing:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for done, (page, text) in enumerate(pool.map(ocr_page,
                    [(str(path), page, str(cache)) for page in missing]), 1):
                texts[page - 1] = text
                if done % 20 == 0 or done == len(missing):
                    print(f"{path.name[:28]} OCR {done}/{len(missing)}", flush=True)
    readable = sum(len(t.strip()) >= 20 for t in texts)
    if readable < max(1, pages * 0.5):
        raise ValueError(f"too few readable PDF pages ({readable}/{pages}); check OCR permissions")
    title = path.stem.split(" (", 1)[0]
    sections = [f"# {title}", "页码为 PDF 文件物理页码；扫描页文字经 OCR 识别，可能有识别误差。"]
    for page, text in enumerate(texts, 1):
        body = wrap_text(text).strip()
        if len(text.strip()) < 80:
            body += "\n\n[本页提取文字较少，可能包含封面、图像或未识别的文字。请核对原 PDF，不能将提取内容视为完整正文。]"
        sections += [f"## PDF 第{page}页", body.strip()]
    return "\n\n".join(sections) + "\n", {"title": title, "pages": pages,
            "ocr_pages": len(missing), "readable_pages": readable,
            "short_pages": [i+1 for i,t in enumerate(texts) if len(t.strip()) < 80]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    report = []
    for source in sorted(args.source.glob("*")):
        if source.suffix.lower() not in {".pdf", ".epub"}:
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if source.suffix.lower() == ".epub":
            text, metadata = epub_text(source)
        else:
            text, metadata = pdf_text(source, args.cache / digest, args.workers)
        name = metadata["title"].replace("/", "_") + ".md"
        if any(r["output"] == name for r in report):
            raise ValueError(f"duplicate book title: {name}")
        (args.output / name).write_text(text, encoding="utf-8")
        report.append({"source": source.name, "sha256": digest, "output": name,
                       "chars": len(text), **metadata})
        print(json.dumps(report[-1], ensure_ascii=False), flush=True)
    if not report:
        raise SystemExit("no PDF/EPUB books found")
    (args.output / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
