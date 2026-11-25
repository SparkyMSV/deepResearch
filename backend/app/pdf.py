"""PDF store for one research task: download once, OCR pages to text on demand (cached), contents and page offset.

Only text ever leaves this module; no PDF goes to a model.
"""
import asyncio
import re
import logging
import os
import threading
import tempfile
from typing import NamedTuple

import indra_ocr
import pypdfium2 as pdfium
import requests

from app import config
from app.search import _HEADERS

log = logging.getLogger(__name__)

MAX_BYTES = 50_000_000
HEAD_PAGES = 12  # contents are looked for in these
MAX_ENTRIES, RAW_LINES, LOCATE_SPAN = 60, 40, 15
_conv = None
_lock = threading.Lock()  # converter/pdfium are not assumed thread-safe


def _converter():
    global _conv
    if _conv is None:
        _conv = indra_ocr.load(mode="auto")
    return _conv


def _download(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=config.FETCH_TIMEOUT, headers=_HEADERS) as r:  # BSE etc. 403 bots
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("pdf exceeds 50 MB cap")
                f.write(chunk)


def _ocr(path: str, n: int) -> tuple[str, int]:
    """Text of 1-based page n (indra-ocr markdown) and the page count."""
    with _lock:
        doc = pdfium.PdfDocument(path)
        try:
            return (_converter().pdf_page(path, doc, n - 1).markdown or "") if n else "", len(doc)
        finally:
            doc.close()


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                         "|": " ", "*": " ", "\ufb01": "fi", "\ufb02": "fl"})
_ENTRY = re.compile(r"^(.{3,80}?)[\s.\u00b7\u2026_-]+(\d{1,4})\s*$")  # "Title ..... 12", "Title 12"
_ENTRY_NUM_FIRST = re.compile(r"^(\d{1,4})\s+(.{3,80})$")  # "12 Title" (multi-column layouts)


def _entry(line: str) -> tuple[str, int] | None:
    if m := _ENTRY.match(line):
        title, n = m.group(1), m.group(2)
    elif m := _ENTRY_NUM_FIRST.match(line):
        n, title = m.group(1), m.group(2)
    else:
        return None
    title = title.strip(" .|*\u00b7\u2026_-")
    # not a title: page ranges ("124-274"), addresses ("Mumbai - 400 051"), years ("Annual Report 2025-26")
    if sum(c.isalpha() for c in title) < 3 or title[-1].isdigit() or re.search(r"\b(19|20)\d\d\b", f"{title} {n}"):
        return None  # also running headers ("10 ANNUAL REPORT 2025-26 ...")
    return title, int(n)


def parse_contents(pages: list[str]) -> list[tuple[str, int]]:
    """Contents entries in the contents' own order, deduplicated, from the pages listing the most of them (at
    least half the best page's count, and pages beyond their own count: footnotes run 1, 2, 3 ...)."""
    per = [[e for ln in t.splitlines() if (e := _entry(ln.strip(" #*|")))] for t in pages]
    per = [es for es in per if len(es) >= 3 and max(n for _, n in es) > len(es)]
    best = max(map(len, per), default=0)
    found = list(dict.fromkeys(e for es in per if 2 * len(es) >= best for e in es))
    return found if len(found) >= 5 else []


def norm(s: str) -> str:
    """Whitespace collapsed and curly quotes straightened, for verbatim checks."""
    s = re.sub(r"<[^>]+>", " ", s or "")  # indra-ocr writes tables as HTML (<td>…</td>)
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)  # words hyphenated across a line break
    s = re.sub(r"(?m)^\s*[-:| ]{3,}\s*$", " ", s)  # markdown table rule lines
    return " ".join(s.translate(_QUOTES).split())


def _layer(path: str) -> dict[int, str]:
    with _lock:
        doc = pdfium.PdfDocument(path)
        try:
            return {i + 1: doc[i].get_textpage().get_text_range() for i in range(len(doc))}
        finally:
            doc.close()


def _spread(path: str) -> bool:
    """Most pages landscape: each PDF page holds two printed pages side by side."""
    with _lock:
        doc = pdfium.PdfDocument(path)
        try:
            wide = sum(1 for i in range(len(doc)) if (lambda w, h: w > 1.3 * h)(*doc[i].get_size()))
            return wide > len(doc) / 2
        finally:
            doc.close()


class Contents(NamedTuple):
    entries: list[tuple[str, int]]  # (title, printed page)
    raw: str  # first lines of the opening pages, when no contents page was found ("" otherwise)


def _scan(path: str, terms: list[str]) -> dict[str, list[int]]:
    """1-based pages whose text layer mentions each term (scanned PDFs have no text layer: empty result)."""
    hits: dict[str, list[int]] = {}
    with _lock:
        doc = pdfium.PdfDocument(path)
        try:
            for i in range(len(doc)):
                text = " ".join(doc[i].get_textpage().get_text_range().lower().split())
                for t in terms:
                    if t in text:
                        hits.setdefault(t, []).append(i + 1)
        finally:
            doc.close()
    return hits


class PdfStore:
    def __init__(self):
        self._files: dict[str, str] = {}
        self._count: dict[str, int] = {}
        self._text: dict[tuple[str, int], str] = {}

    async def pages(self, url: str) -> int:
        if url not in self._files:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.close()
            try:
                log.info("pdf %s: downloading", url)
                await asyncio.to_thread(_download, url, tmp.name)
                _, n = await asyncio.to_thread(_ocr, tmp.name, 0)
            except BaseException:
                os.unlink(tmp.name)
                raise
            self._files[url], self._count[url] = tmp.name, n
            log.info("pdf %s: %.1f MB, %d pages", url, os.path.getsize(tmp.name) / 1e6, n)
        return self._count[url]

    async def text(self, url: str, n: int) -> str:
        if (url, n) not in self._text:
            await self.pages(url)
            self._text[(url, n)], _ = await asyncio.to_thread(_ocr, self._files[url], n)
        return self._text[(url, n)]

    async def contents(self, url: str) -> Contents:
        layer = await self.text_layer(url)
        head = [layer.get(i) if (layer.get(i) or "").strip() else await self.text(url, i)  # OCR scanned pages only
                for i in range(1, min(await self.pages(url), HEAD_PAGES) + 1)]
        if entries := parse_contents(head):
            return Contents(entries[:MAX_ENTRIES], "")
        lines = [ln.strip(" #*|") for t in head for ln in t.splitlines() if ln.strip(" #*|")]
        return Contents([], "\n".join(lines[:RAW_LINES]))

    async def spread(self, url: str) -> bool:
        await self.pages(url)
        return await asyncio.to_thread(_spread, self._files[url])

    async def locate(self, url: str, title: str, printed: int) -> tuple[int, bool]:
        """PDF page where a contents entry starts: printed page, then +1, -1, +2, ... up to LOCATE_SPAN."""
        n, want = await self.pages(url), norm(title).lower()
        for d in range(LOCATE_SPAN + 1):
            for p in ((printed,) if d == 0 else (printed + d, printed - d)):
                if 1 <= p <= n and want in norm(await self.text(url, p)).lower():
                    return p, True
        return min(max(printed, 1), n), False

    async def find(self, url: str, terms: list[str]) -> dict[str, list[int]]:
        """Pages mentioning each term, from the PDF's text layer (fast; no OCR)."""
        await self.pages(url)
        return await asyncio.to_thread(_scan, self._files[url], [t.lower() for t in terms if t])

    async def text_layer(self, url: str) -> dict[int, str]:
        """Every page's built-in text (no OCR; empty for scanned pages), cached per document."""
        cache = self.__dict__.setdefault("_layers", {})
        if url not in cache:
            await self.pages(url)
            cache[url] = await asyncio.to_thread(_layer, self._files[url])
        return cache[url]

    def close(self) -> None:
        for path in self._files.values():
            try:
                os.unlink(path)
            except OSError:
                pass
        self._files.clear()
