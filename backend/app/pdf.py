"""PDF reader: the swap point for the future OCR/retrieval redesign.

Only `read_pdf` is public; the task engine calls it for every PDF search hit.
"""
import os
import tempfile
from datetime import datetime, timezone

import indra_ocr
import pypdfium2 as pdfium
import requests

from app import config
from app.cloud_llm import cloud
from app.db import Evidence
from app.local_llm import parse_json

MAX_BYTES = 50_000_000
TRIAGE_PAGES = 8
MAX_PAGES = 30
_conv = None


def _converter():
    global _conv
    if _conv is None:
        _conv = indra_ocr.load(mode="auto")
    return _conv


def _download(url: str, path: str) -> None:
    r = requests.get(url, stream=True, timeout=config.FETCH_TIMEOUT)
    r.raise_for_status()
    size = 0
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("pdf exceeds 50 MB cap")
            f.write(chunk)


def _read_pages(path: str, wanted: list[int] | None) -> tuple[dict[int, str], int]:
    """Markdown by 1-based page (first TRIAGE_PAGES if wanted is None), and page count."""
    doc = pdfium.PdfDocument(path)
    try:
        n = len(doc)
        nums = range(1, min(n, TRIAGE_PAGES) + 1) if wanted is None else wanted
        conv = _converter()
        return {i: conv.pdf_page(path, doc, i - 1).markdown for i in nums}, n
    finally:
        doc.close()


def _page_list(ranges, n: int) -> list[int]:
    pages = []
    for r in ranges if isinstance(ranges, list) else []:
        if isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r):
            pages += [p for p in range(max(r[0], 1), min(r[1], n) + 1) if p not in pages]
    return pages[:MAX_PAGES]


def _tagged(pages: dict[int, str]) -> str:
    return "\n\n".join(f"[page {i}]\n{t}" for i, t in pages.items())


async def read_pdf(url: str, objective: str, subtask_id: int, loop: int, conn) -> list[Evidence]:
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    try:
        _download(url, tmp.name)
        head, n = _read_pages(tmp.name, None)
        triage = await cloud("pdf", [{"role": "user", "content": (
            f"Objective: {objective}\nThe PDF has {n} pages. Here are the first {len(head)}:\n\n{_tagged(head)}\n\n"
            'Using the contents, pick the pages most likely to answer the objective. '
            'Reply as JSON only: {"pages": [[start, end], ...]} (1-based, inclusive).')}], conn)
        pages = _page_list((parse_json(triage) or {}).get("pages"), n)
        if not pages:
            return []
        body, _ = _read_pages(tmp.name, pages)
        raw = await cloud("pdf", [{"role": "user", "content": (
            f"Objective: {objective}\n\n{_tagged(body)}\n\n"
            "Extract facts relevant to the objective, quoting figures exactly. Reply as JSON only: "
            '{"facts": [{"fact": "...", "page": N, "period": "reporting period or null"}]}')}], conn)
    finally:
        os.unlink(tmp.name)
    now = datetime.now(timezone.utc).date().isoformat()
    title = url.split("?")[0].rsplit("/", 1)[-1] or url
    facts = (parse_json(raw) or {}).get("facts")
    return [Evidence(subtask_id, loop, url, title, f["fact"], f.get("period") or None, now, f.get("page"))
            for f in facts if isinstance(f, dict) and f.get("fact")] if isinstance(facts, list) else []
