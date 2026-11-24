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
HEAD_PAGES = 8  # contents are looked for in these
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


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_ENTRY = re.compile(r"^(.{3,80}?)[\s.\u00b7\u2026_-]{2,}(\d{1,4})\s*$")


def norm(s: str) -> str:
    """Whitespace collapsed and curly quotes straightened, for verbatim checks."""
    return " ".join((s or "").translate(_QUOTES).split())


class Contents(NamedTuple):
    entries: list[tuple[str, int]]  # (title, printed page)
    raw: str  # first lines of the opening pages, when no contents page was found ("" otherwise)


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
        head = [await self.text(url, i) for i in range(1, min(await self.pages(url), HEAD_PAGES) + 1)]
        lines = [ln.strip(" #*|") for t in head for ln in t.splitlines() if ln.strip(" #*|")]
        entries = [(m.group(1).strip(" .|*"), int(m.group(2))) for ln in lines if (m := _ENTRY.match(ln))]
        if len(entries) >= 5:
            return Contents(entries[:MAX_ENTRIES], "")
        return Contents([], "\n".join(lines[:RAW_LINES]))

    async def locate(self, url: str, title: str, printed: int) -> tuple[int, bool]:
        """PDF page where a contents entry starts: printed page, then +1, -1, +2, ... up to LOCATE_SPAN."""
        n, want = await self.pages(url), norm(title).lower()
        for d in range(LOCATE_SPAN + 1):
            for p in ((printed,) if d == 0 else (printed + d, printed - d)):
                if 1 <= p <= n and want in norm(await self.text(url, p)).lower():
                    return p, True
        return min(max(printed, 1), n), False

    def close(self) -> None:
        for path in self._files.values():
            try:
                os.unlink(path)
            except OSError:
                pass
        self._files.clear()
