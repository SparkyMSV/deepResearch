import asyncio

import pytest

from app import docprep
from tests.fakes import FakeStore

U = "http://x/ar.pdf"


def prep(pages, toc=None, scanned=False):
    s = FakeStore({U: pages}, {U: toc or []})
    if scanned:
        s.scanned = {U}
    return asyncio.run(docprep.prepare(s, U))


def doc_with_offset_two():
    pages = [f"filler {i}" for i in range(1, 31)]
    toc = [("Directors report", 4), ("Financial statements", 10), ("Notes to accounts", 15), ("Risk management", 20),
           ("Corporate governance", 25)]
    for t, p in toc:
        pages[p + 2 - 1] = f"{t}\nbody text"
    pages[13] = "Financial statements continued: total borrowings were 6,100 crore at 31 March 2026"
    return pages, toc


def test_offset_from_contents():
    p = prep(*doc_with_offset_two())
    assert p.offset_ok and [e["pdf"] for e in p.contents] == [6, 12, 17, 22, 27]
    assert docprep.to_pdf(p, 11) == 13


def test_sections_restarting_numbering_are_ambiguous():
    pages = [f"filler {i}" for i in range(1, 31)]
    toc = [("Board report", 3), ("Standalone statements", 1), ("Standalone notes", 6), ("Consolidated statements", 1),
           ("Consolidated notes", 6)]
    for (t, _), pdf in zip(toc, (5, 10, 15, 20, 25)):
        pages[pdf - 1] = f"{t}\nbody"
    p = prep(pages, toc)
    assert docprep.to_pdf(p, 2) == ["Standalone statements", "Consolidated statements"]
    assert docprep.to_pdf(p, 2, section="consolidated") == 21
    with pytest.raises(ValueError):
        docprep.to_pdf(p, 999)


def test_footer_numbers_when_no_contents():
    pages = ["cover", "letter", "intro"] + [f"Some text on this page\n{i}" for i in range(1, 28)]
    p = prep(pages)
    assert p.offset_ok and docprep.to_pdf(p, 10) == 13


def test_unverified_offset_zero():
    p = prep([f"plain page {i}" for i in range(1, 11)])
    assert not p.offset_ok and docprep.to_pdf(p, 5) == 5


def test_to_printed_inverse():
    p = prep(*doc_with_offset_two())
    assert docprep.to_printed(p, 13) == 11 and docprep.to_printed(p, 1) is None


def test_locate_ranks_the_page_with_the_figure_first():
    p = prep(*doc_with_offset_two())
    hits = p.index.locate("total borrowings March 2026")
    assert hits[0]["printed"] == 12 and hits[0]["pdf"] == 14 and "6,100" in hits[0]["passage"]
    assert hits[0]["section"] == "Financial statements"


def test_locate_one_anchor_per_page():
    pages = ["revenue revenue revenue " * 80, "revenue once", "nothing"]
    hits = prep(pages).index.locate("revenue")
    assert [h["pdf"] for h in hits] == [1, 2]


def test_scanned_pages_not_indexed():
    p = prep([f"page {i}" for i in range(1, 6)], scanned=True)
    assert p.scanned == 5 and p.index.locate("anything") == []
