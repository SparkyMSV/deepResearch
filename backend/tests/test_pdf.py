import asyncio
import json
import time

import fitz
import pytest

from app import pdf
from app.db import connect

URL = "http://x.org/reports/annual.pdf"


@pytest.fixture(scope="module")
def pdf_bytes():
    d = fitz.open()
    for i in range(1, 13):
        text = "Contents ... Results p.9" if i == 1 else "Revenue 120" if i == 9 else f"filler text page {i}"
        d.new_page().insert_text((72, 72), text)
    return d.tobytes()


class Resp:
    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]


def run(monkeypatch, pdf_bytes, triage, facts=None):
    monkeypatch.setattr(pdf.requests, "get", lambda *a, **k: Resp(pdf_bytes))
    prompts = []

    async def fake_cloud(role, messages, conn):
        assert role == "pdf"
        prompts.append(messages[-1]["content"])
        return triage if len(prompts) == 1 else (facts or '{"facts": []}')

    monkeypatch.setattr(pdf, "cloud", fake_cloud)
    out, about = asyncio.run(pdf.read_pdf(URL, "revenue", 1, 0, connect(":memory:")))
    assert about.endswith("(12 pages)")
    return out, prompts


def test_reads_only_triaged_pages(monkeypatch, pdf_bytes):
    _, prompts = run(monkeypatch, pdf_bytes, '{"pages": [[9, 9]]}')
    assert "[page 9]" in prompts[1] and "[page 3]" not in prompts[1]


def test_page_cap(monkeypatch, pdf_bytes):
    _, prompts = run(monkeypatch, pdf_bytes, '{"pages": [[1, 500]]}')
    assert prompts[1].count("[page ") == 12  # clamped to doc
    monkeypatch.setattr(pdf, "MAX_PAGES", 5)
    _, prompts = run(monkeypatch, pdf_bytes, '{"pages": [[1, 500]]}')
    assert prompts[1].count("[page ") == 5


def test_facts_to_evidence(monkeypatch, pdf_bytes):
    facts = json.dumps({"facts": [{"fact": "Revenue 120", "page": 9, "period": "FY24"}]})
    out, _ = run(monkeypatch, pdf_bytes, '{"pages": [[9, 9]]}', facts)
    assert (out[0].page, out[0].url, out[0].passage, out[0].published) == (9, URL, "Revenue 120", "FY24")
    assert out[0].title == "annual.pdf"


def test_bad_triage_json_returns_empty(monkeypatch, pdf_bytes):
    out, prompts = run(monkeypatch, pdf_bytes, "not json")
    assert out == [] and len(prompts) == 1


def test_event_loop_stays_responsive(monkeypatch, pdf_bytes):
    monkeypatch.setattr(pdf.requests, "get", lambda *a, **k: Resp(pdf_bytes))
    monkeypatch.setattr(pdf, "_read_pages", lambda p, w: (time.sleep(0.3) or {1: "x"}, 12))

    async def fake_cloud(role, messages, conn):
        return '{"pages": []}'

    monkeypatch.setattr(pdf, "cloud", fake_cloud)
    ticks = 0

    async def main():
        nonlocal ticks
        task = asyncio.create_task(pdf.read_pdf(URL, "o", 1, 0, connect(":memory:")))
        while not task.done():
            await asyncio.sleep(0.05)
            ticks += 1
        await task

    asyncio.run(main())
    assert ticks >= 3


def test_triage_says_what_the_document_is(monkeypatch, pdf_bytes):
    monkeypatch.setattr(pdf.requests, "get", lambda *a, **k: Resp(pdf_bytes))

    async def fake_cloud(role, messages, conn):
        return '{"about": "Acme Ltd annual report FY2024", "pages": []}'

    monkeypatch.setattr(pdf, "cloud", fake_cloud)
    out, about = asyncio.run(pdf.read_pdf(URL, "revenue", 1, 0, connect(":memory:")))
    assert out == [] and about == "Acme Ltd annual report FY2024 (12 pages)"
