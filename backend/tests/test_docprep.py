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


def test_far_section_with_restarted_numbering_is_found_between_neighbours():
    pages = [f"filler {i}" for i in range(1, 221)]
    toc = [("Board report", 3), ("Standalone statements", 1), ("Standalone notes", 6), ("Consolidated statements", 1),
           ("Consolidated notes", 6)]
    pages[1] = "Contents\n" + "\n".join(f"{t} ..... {p}" for t, p in toc)
    for (t, _), pdf in zip(toc, (5, 10, 15, 200, 205)):
        pages[pdf - 1] = f"{t}\nbody"
    p = prep(pages, toc)
    assert [e["pdf"] for e in p.contents] == [5, 10, 15, 200, 205]
    assert docprep.to_pdf(p, 2, section="consolidated statements") == 201
    assert docprep.to_printed(p, 201) == 2


def test_contents_page_does_not_match_its_own_titles():
    pages = [f"filler {i}" for i in range(1, 31)]
    toc = [("Corporate overview", 2), ("Chairman message", 4), ("Directors report", 8), ("Financial statements", 14),
           ("Notes to accounts", 20)]
    pages[1] = "Contents\n" + "\n".join(f"{t} ..... {p}" for t, p in toc)
    for t, p in toc:
        pages[p + 2 - 1] = f"{t}\nbody"
    p = prep(pages, toc)
    assert [e["pdf"] for e in p.contents] == [4, 6, 10, 16, 22]


def test_scanned_pdf_keeps_its_contents_unverified():
    toc = [("Directors report", 4), ("Financial statements", 10), ("Notes", 15), ("Risk", 20), ("Governance", 25)]
    p = prep([f"page {i}" for i in range(1, 31)], toc, scanned=True)
    assert [e["title"] for e in p.contents][:2] == ["Directors report", "Financial statements"]
    assert not p.offset_ok and docprep.to_pdf(p, 10) == 10


def test_exact_section_name_wins():
    pages = [f"filler {i}" for i in range(1, 31)]
    toc = [("Board report", 3), ("Notes", 1), ("Standalone notes", 6), ("Notes to consolidated accounts", 1),
           ("Annexure", 6)]
    for (t, _), pdf in zip(toc, (5, 10, 15, 20, 25)):
        pages[pdf - 1] = f"{t}\nbody"
    assert docprep.to_pdf(prep(pages, toc), 2, section="Notes") == 11


def test_two_page_spreads_map_printed_pages_in_pairs():
    # PDF 1-2 single pages (letter), then spreads: PDF 3 = printed 2-3, PDF 4 = 4-5, ...
    pages = ["Submission of annual report", "blank"] + [f"spread {2 * (i - 2)}-{2 * (i - 2) + 1}" for i in range(3, 103)]
    toc = [("The Journey", 2), ("About Us", 22), ("Statutory Reports", 80), ("Financial Statements", 120),
           ("Valuation Report", 180)]
    for t, pr in toc:
        pages[pr // 2 + 2 - 1] = f"{t}\nbody"
    s = FakeStore({U: pages}, {U: toc})
    s.spreads = {U}
    p = asyncio.run(docprep.prepare(s, U))
    assert p.spread and p.offset_ok and [e["pdf"] for e in p.contents] == [3, 13, 42, 62, 92]
    assert docprep.to_pdf(p, 150) == 77 and docprep.to_pdf(p, 151) == 77 and docprep.to_pdf(p, 152) == 78
    assert docprep.to_printed(p, 77) == 150 and docprep.to_printed(p, 62) == 120


def test_scrambled_contents_are_placed_in_page_order():
    pages, toc = doc_with_offset_two()
    scrambled = toc[3:] + toc[:3]  # multi-column contents read right column first
    p = prep(pages, scrambled)
    assert [(e["title"], e["pdf"]) for e in p.contents] == [(t, pr + 2) for t, pr in toc]


def test_one_word_title_not_matched_far_from_its_page():
    pages, toc = doc_with_offset_two()
    pages[27] = "Strength of our balance sheet\nbody"
    p = prep(pages, toc[:2] + [("Strength", 12)] + toc[2:])
    assert [e["pdf"] for e in p.contents] == [6, 12, None, 17, 22, 27]


def test_printed_page_number_corrects_a_title_match_elsewhere():
    pages, toc = doc_with_offset_two()
    pages[11] = "Independent auditor's report\nbody\n10"  # the real start: no title on top, page number in the footer
    pages[19] = "Financial statements of the subsidiaries\nbody"  # a later page starting with the same words
    pages[16] = "Notes overview\nbody\n15"  # an entry whose title isn't on top: placed by its page number
    p = prep(pages, toc)
    assert [e["pdf"] for e in p.contents] == [6, 12, 17, 22, 27]
