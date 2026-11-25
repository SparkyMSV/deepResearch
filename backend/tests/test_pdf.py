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


def test_norm_strips_html_table_tags():
    html = "<table><tr><td>Revenue from operations</td><td>24</td><td>2,110,127</td></tr></table>"
    assert pdf.norm(html) == "Revenue from operations 24 2,110,127"


def test_text_layer_reads_every_page_without_ocr(store, monkeypatch):
    real = pdf._ocr
    monkeypatch.setattr(pdf, "_ocr", lambda path, n: real(path, n) if n == 0 else (_ for _ in ()).throw(AssertionError("OCR")))
    layer = asyncio.run(store.text_layer(URL))
    assert len(layer) == 12 and "Asset quality improved" in layer[6]


NEXUS_TOC = """Market Capitalization
(as of March 30, 2026) `22,845 crores
BSE Code 543913
Across
the Pages
392 Financial Statements
Management
Discussion and
Analysis
210
584 Valuation Report
244 Statutory Reports
02 The Journey of Nexus
10 Corporate Information
12 Chairman's Note
22 About Us
Tuhin Parikh
(till November 04, 2025)"""

MINDSPACE_TOC = """Mindspace Business Parks REIT | Annual Report 2025-26
Mumbai - 400 051
124-274
Statutory Disclosures 144
Key Debt Covenants 155
Risk Factors 156
322-536
Standalone 322
Consolidated 392
02-123
How to Navigate the Report 02
Who We Are 06
Defining Moments of the Year 08"""


def test_contents_number_first_lines():
    e = pdf.parse_contents(["Cover", NEXUS_TOC])
    assert e == [("Financial Statements", 392), ("Valuation Report", 584), ("Statutory Reports", 244),
                 ("The Journey of Nexus", 2), ("Corporate Information", 10), ("Chairman's Note", 12), ("About Us", 22)]


def test_contents_single_space_titles_skip_years_and_ranges():
    e = pdf.parse_contents([MINDSPACE_TOC, "Who We Are 06\nHow to Navigate the Report 02\nnothing else"])
    assert e == [("Statutory Disclosures", 144), ("Key Debt Covenants", 155), ("Risk Factors", 156),
                 ("Standalone", 322), ("Consolidated", 392), ("How to Navigate the Report", 2), ("Who We Are", 6),
                 ("Defining Moments of the Year", 8)]


def test_contents_with_restarted_numbering_keep_their_order():
    toc = "Board report .... 3\nStandalone statements .... 1\nStandalone notes .... 6\n" \
          "Consolidated statements .... 1\nConsolidated notes .... 6"
    assert [p for _, p in pdf.parse_contents([toc])] == [3, 1, 6, 1, 6]


def test_contents_read_from_text_layer_without_ocr(store, monkeypatch):
    real, used = pdf._ocr, []
    monkeypatch.setattr(pdf, "_ocr", lambda path, n: used.append(n) or real(path, n))
    assert len(asyncio.run(store.contents(URL)).entries) == 5 and used == [0]  # page count only


def test_spread_pages_detected(monkeypatch):
    d = fitz.open()
    d.new_page(width=595, height=842).insert_text((72, 72), "cover")
    for i in range(4):
        d.new_page(width=1191, height=842).insert_text((72, 72), f"spread {i}")
    monkeypatch.setattr(pdf.requests, "get", lambda url, **k: Resp(d.tobytes()))
    s = pdf.PdfStore()
    assert asyncio.run(s.spread(URL)) is True
    s.close()


def test_single_pages_are_not_spreads(store):
    assert asyncio.run(store.spread(URL)) is False


def test_contents_ignore_running_headers_and_footnote_pages():
    header = "10 ANNUAL REPORT 2025-26 NEXUS SELECT TRUST"
    footnotes = "\n".join(f"{i} Based on independent valuer assessment {i}a" if i % 2 else f"{i} Excludes item {i}x"
                          for i in range(1, 6)) + "\n1 Market Value of Mindspace\n2 Includes One Radial\n3 Buildings"
    e = pdf.parse_contents([header, NEXUS_TOC + "\n" + header, footnotes])
    assert [p for _, p in e] == [392, 584, 244, 2, 10, 12, 22]  # the contents page only, in its own order
