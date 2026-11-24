import asyncio

import fitz
import pytest
import requests

from app import pdf

URL = "http://x.org/reports/annual.pdf"
TOC = ["Directors report ..... 4", "Financial statements ..... 7", "Notes to accounts ..... 9",
       "Risk management ..... 10", "Corporate governance ..... 11"]


def make_pdf(pages):
    d = fitz.open()
    for text in pages:
        page = d.new_page()
        for i, line in enumerate(text.split("\n")):
            page.insert_text((72, 72 + 14 * i), line)
    return d.tobytes()


@pytest.fixture(scope="module")
def report():
    pages = [f"filler page {i}" for i in range(1, 13)]
    pages[1] = "Contents\n" + "\n".join(TOC)
    pages[5] = "Directors report\nAsset quality improved."  # printed page 4 is PDF page 6
    return make_pdf(pages)


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


@pytest.fixture
def store(monkeypatch, report):
    gets = []
    monkeypatch.setattr(pdf.requests, "get", lambda url, **k: gets.append(url) or Resp(report))
    s = pdf.PdfStore()
    s.gets = gets
    yield s
    s.close()


def test_downloads_once_and_caches_pages(store, monkeypatch):
    calls = []
    real = pdf._ocr
    monkeypatch.setattr(pdf, "_ocr", lambda path, n: calls.append(n) or real(path, n))

    async def go():
        assert await store.pages(URL) == 12
        a = await store.text(URL, 6)
        assert a == await store.text(URL, 6) and "Asset quality" in a

    asyncio.run(go())
    assert store.gets == [URL] and calls.count(6) == 1


def test_contents_entries(store):
    c = asyncio.run(store.contents(URL))
    assert c.raw == "" and c.entries[0] == ("Directors report", 4) and len(c.entries) == 5


def test_contents_falls_back_to_raw_lines(monkeypatch):
    monkeypatch.setattr(pdf.requests, "get", lambda url, **k: Resp(make_pdf(["Cover\nAcme Ltd", "Intro text"])))
    s = pdf.PdfStore()
    c = asyncio.run(s.contents(URL))
    s.close()
    assert c.entries == [] and "Acme Ltd" in c.raw


def test_locate_finds_offset_page(store):
    assert asyncio.run(store.locate(URL, "Directors report", 4)) == (6, True)
    assert asyncio.run(store.locate(URL, "Nothing like this", 4)) == (4, False)


def test_download_failure_raises(monkeypatch):
    def boom(url, **k):
        raise requests.HTTPError("403")

    monkeypatch.setattr(pdf.requests, "get", boom)
    with pytest.raises(requests.HTTPError):
        asyncio.run(pdf.PdfStore().pages(URL))


def test_norm_straightens_quotes():
    assert pdf.norm("Company’s  GNPA\nwas") == "Company's GNPA was"
