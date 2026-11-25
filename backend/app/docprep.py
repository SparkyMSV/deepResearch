"""Background document preparation: contents, per-section page offsets and a page index (BM25), so the PDF
agent can work in printed page numbers and find anchors before reading. No model involved."""
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from app.pdf import norm

log = logging.getLogger(__name__)

SEARCH_BEFORE, SEARCH_AFTER, MAX_GAP = 5, 25, 400
PASSAGE, OVERLAP, K1, B = 600, 100, 1.5, 0.75
_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class PageIndex:
    """BM25 over ~600-character passages of each page's text layer; one best passage per page."""

    def __init__(self, passages: list[dict]):
        self.passages = passages  # {"pdf","printed","section","text","tokens"}
        self.df = Counter(t for p in passages for t in set(p["tokens"]))
        self.avg = sum(len(p["tokens"]) for p in passages) / len(passages) if passages else 0

    @classmethod
    def build(cls, pages: dict[int, str], prep: "Prepared") -> "PageIndex":
        out = []
        for pdf_page, text in sorted(pages.items()):
            text = " ".join(text.split())
            if not text:
                continue
            for start in range(0, max(len(text) - OVERLAP, 1), PASSAGE - OVERLAP):
                chunk = text[start:start + PASSAGE]
                out.append({"pdf": pdf_page, "printed": to_printed(prep, pdf_page), "section": _section(prep, pdf_page),
                            "text": chunk, "tokens": _tokens(chunk)})
        return cls(out)

    def locate(self, query: str, k: int = 6) -> list[dict]:
        q = set(_tokens(query))
        if not q or not self.passages:
            return []
        n = len(self.passages)
        best: dict[int, tuple[float, dict]] = {}
        for p in self.passages:
            tf = Counter(p["tokens"])
            score = 0.0
            for t in q:
                if tf[t]:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    score += idf * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * len(p["tokens"]) / self.avg))
            if score > 0 and score > best.get(p["pdf"], (0, None))[0]:
                best[p["pdf"]] = (score, p)
        ranked = sorted(best.values(), key=lambda x: -x[0])[:k]
        return [{"printed": p["printed"], "pdf": p["pdf"], "section": p["section"], "passage": _snippet(p["text"], q)}
                for _, p in ranked]


def _snippet(text: str, q: set[str], width: int = 220) -> str:
    low = text.lower()
    hits = [i for t in q if (i := low.find(t)) >= 0]
    start = max(0, min(hits) - 60) if hits else 0
    return text[start:start + width].strip()


@dataclass
class Prepared:
    pages: int
    contents: list[dict] = field(default_factory=list)  # {"title","printed","pdf"} in document order
    offset: int = 0  # whole-document offset when there are no contents
    offset_ok: bool = False
    scanned: int = 0  # pages without a text layer (not indexed)
    spread: bool = False  # each PDF page holds two printed pages (2-3, 4-5, ...)
    index: PageIndex | None = None
    headers: set[str] = field(default_factory=set)  # running header/footer lines, digits masked
    notes: list[dict] = field(default_factory=list)  # {"no","title","pdf","section"}: first page of each note


NOTE_MAX, REPEAT_GAP, NOTED = 150, 10, 10  # highest note number; pages; notes that make a section "noted"
_NOTE = re.compile(r"^(\d{1,3}[A-Z]?)\s*[.:)]?\s+([A-Z][A-Z0-9 ,&()/'.:-]{3,70})$")  # "17 BORROWINGS"


def _notes(prep: "Prepared", layer: dict[int, str]) -> list[dict]:
    """Note headings (number + capitals) in the text layer: the first page of each note. A heading repeated
    within REPEAT_GAP pages is the same note continued; further on (standalone, then consolidated) it's a new one.
    Running headers ("392 NEXUS SELECT TRUST 393") are skipped."""
    out, last = [], {}
    for p, text in sorted(layer.items()):
        sec = _section(prep, p)
        for ln in (text or "").splitlines():
            m = _NOTE.match(ln.strip())
            if not m or int(re.sub(r"\D", "", m.group(1))) > NOTE_MAX or m.group(2)[-1].isdigit() \
                    or "CONTINUED" in m.group(2).upper():
                continue
            key = (sec, m.group(1), m.group(2).strip())
            if key not in last or p - last[key] > REPEAT_GAP:
                out.append({"no": m.group(1), "title": m.group(2).strip(), "pdf": p, "section": sec})
            last[key] = p
    return out


def notes(prep: "Prepared", section: str) -> list[tuple[str, str, int | None]]:
    """(note number, title, printed page) for one section (exact name first, else names containing it)."""
    want = section.strip().lower()
    names = {n["section"] for n in prep.notes if n["section"]}
    pick = [x for x in names if x.lower() == want] or [x for x in names if want in x.lower()]
    return [(n["no"], n["title"], to_printed(prep, n["pdf"])) for n in prep.notes if n["section"] in pick]


def _h(prep: Prepared, printed: int) -> int:
    """Printed page in PDF-page steps: spreads advance one PDF page per two printed pages."""
    return printed // 2 if prep.spread else printed


def _runs(prep: Prepared) -> list[tuple[dict, int]]:
    """(contents entry, PDF page where the next entry starts) for each located entry."""
    es = [e for e in prep.contents if e.get("pdf")]
    return [(e, es[i + 1]["pdf"] if i + 1 < len(es) else prep.pages + 1) for i, e in enumerate(es)]


def _section(prep: Prepared, pdf_page: int) -> str | None:
    return next((e["title"] for e, end in _runs(prep) if e["pdf"] <= pdf_page < end), None)


def to_pdf(prep: Prepared, printed: int, section: str | None = None):
    """PDF page for a printed page; a list of section titles when the number exists in several sections
    (numbering restarts); ValueError when out of range or before the first contents entry."""
    runs = _runs(prep)
    if runs:
        cands = [(e["title"], e["pdf"] + _h(prep, printed) - _h(prep, e["printed"])) for e, end in runs
                 if printed >= e["printed"] and e["pdf"] + _h(prep, printed) - _h(prep, e["printed"]) < end]
        if section:
            exact = [c for c in cands if c[0].lower() == section.strip().lower()]
            cands = exact or [c for c in cands if section.strip().lower() in c[0].lower()]
            if not cands:
                raise ValueError(f"no section {section!r} has a page {printed}; sections: "
                                 + "; ".join(e["title"] for e, _ in runs))
        if len(cands) == 1:
            return cands[0][1]
        if len(cands) > 1:
            return [t for t, _ in cands]
        raise ValueError(f"page {printed} is not in a section of the contents")
    p = _h(prep, printed) + prep.offset
    if not 1 <= p <= prep.pages:
        raise ValueError(f"page {printed} is outside the document")
    return p


def to_printed(prep: Prepared, pdf_page: int) -> int | None:
    for e, end in _runs(prep):
        if e["pdf"] <= pdf_page < end:
            return max(e["printed"], 2 * (e["printed"] // 2 + pdf_page - e["pdf"])) if prep.spread \
                else e["printed"] + pdf_page - e["pdf"]
    if prep.contents:
        return None  # front matter
    p = (pdf_page - prep.offset) * (2 if prep.spread else 1)
    return p if p >= 1 else None


def _forward(pdfs: list[int | None], same_ok: bool = False) -> list[int]:
    """Indexes of the longest increasing run of located PDF pages (None = not located); same_ok lets two entries
    share a page (both halves of one spread)."""
    idx = [i for i, p in enumerate(pdfs) if p]
    best: dict[int, list[int]] = {}
    for i in idx:
        best[i] = max((best[j] for j in idx if j < i and (pdfs[j] < pdfs[i] or same_ok and pdfs[j] == pdfs[i])), key=len, default=[]) + [i]
    return max(best.values(), key=len, default=[])


def _mask(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split()))


def _headers(layer: dict[int, str]) -> set[str]:
    """Running header/footer lines with digits masked ("# Annual Report #-#"): those on many pages."""
    count = Counter()
    for t in layer.values():
        count.update({_mask(ln) for ln in (t or "").splitlines()
                      if re.fullmatch(r"\d{1,4}", ln.strip())  # bare page-number footers
                      or 3 < len(ln.strip()) <= 80 and re.search(r"\d", ln) and re.search(r"[A-Za-z]", ln)})
    return {m for m, k in count.items() if k >= max(3, len(layer) // 10)}


def _numbered(text: str, printed: int, spread: bool, headers: set[str]) -> bool:
    """The page prints this page number (or, on a spread, its pair's) in a running header or footer line."""
    nums = {printed, printed ^ 1} if spread else {printed}
    return any(_mask(ln) in headers and nums & {int(x) for x in re.findall(r"(?<![\d.,])\d+(?![\d.,%])", ln)}
               for ln in text.splitlines())


def _starts_with(text: str, title: str) -> bool:
    head = norm(text)[:300].lower()
    return norm(title).lower() in head


async def prepare(store, url: str) -> Prepared:
    """Contents with per-entry PDF pages, offsets, and the page index; cached on the store per URL."""
    cache = store.__dict__.setdefault("_prepared", {})
    if url in cache:
        return cache[url]
    n = await store.pages(url)
    layer = await store.text_layer(url)
    prep = Prepared(pages=n, scanned=sum(1 for i in range(1, n + 1) if not (layer.get(i) or "").strip()),
                    spread=await store.spread(url))
    prep.headers = _headers(layer)
    c = await store.contents(url)

    titles = [t for t, _ in c.entries]
    # the contents page lists the titles itself: never match an entry to a page that names two or more of them
    toc_pages = {p for p, t in layer.items() if sum(_starts_with(t, x) for x in titles) >= 2}

    def find(title: str, lo: int, hi: int) -> int | None:
        return next((p for p in range(max(1, lo), min(n, hi) + 1)
                     if p not in toc_pages and _starts_with(layer.get(p) or "", title)), None)

    def place(entries: list[tuple[str, int]]) -> list[dict]:
        out = [{"title": t, "printed": pr, "pdf": find(t, _h(prep, pr) - SEARCH_BEFORE, _h(prep, pr) + SEARCH_AFTER)}
               for t, pr in entries]
        located = [e for e in out if e["pdf"]]
        if not located:
            return out
        guess = Counter(e["pdf"] - _h(prep, e["printed"]) for e in located).most_common(1)[0][0]
        for i, e in enumerate(out):  # not near its printed page: try around the common offset, then
            if e["pdf"]:             # anywhere between the neighbouring located entries
                continue
            e["pdf"] = find(e["title"], _h(prep, e["printed"]) + guess - SEARCH_BEFORE,
                            _h(prep, e["printed"]) + guess + SEARCH_BEFORE)
            if not e["pdf"] and len(e["title"].split()) > 1:  # one word ("Strength") matches too much text
                prev = max((x["pdf"] for x in out[:i] if x["pdf"]), default=0)
                nxt = min((x["pdf"] for x in out[i + 1:] if x["pdf"]), default=n + 1)
                e["pdf"] = find(e["title"], prev + 1, min(nxt - 1, prev + MAX_GAP))
        keep = set(_forward([e["pdf"] for e in out], prep.spread))  # entries must run forward through the PDF: keep the
        for i, e in enumerate(out):                       # longest such run, drop the outliers
            if i not in keep:
                e["pdf"] = None
        return out

    # multi-column contents pages come out of the text layer scrambled, and restarted numbering looks the same:
    # keep whichever order (the contents' own, or by page number) places more entries
    tries = [place(c.entries), place(sorted(c.entries, key=lambda e: e[1]))]
    prep.contents = max(tries, key=lambda es: sum(1 for e in es if e["pdf"]))
    located = [e for e in prep.contents if e["pdf"]]
    if located:
        guess = Counter(e["pdf"] - _h(prep, e["printed"]) for e in located).most_common(1)[0][0]
        for e in prep.contents:  # the page at the common offset prints the entry's page number: that's its start
            at = _h(prep, e["printed"]) + guess
            if e["pdf"] != at and 1 <= at <= n and _numbered(layer.get(at) or "", e["printed"], prep.spread, prep.headers):
                e["pdf"] = at
        keep = set(_forward([e["pdf"] for e in prep.contents], prep.spread))
        for i, e in enumerate(prep.contents):
            if i not in keep:
                e["pdf"] = None
        prep.offset, prep.offset_ok = guess, True
    else:
        # nothing located (e.g. scanned pages): keep the contents for the agent, numbers unverified
        offs = Counter()
        for p in range(1, n + 1, max(1, n // 12)):  # page numbers printed in footers/headers
            lines = [ln.strip() for ln in (layer.get(p) or "").splitlines() if ln.strip()]
            for ln in lines[:2] + lines[-3:]:
                if re.fullmatch(r"\d{1,4}", ln):
                    offs[p - _h(prep, int(ln))] += 1
        if offs and offs.most_common(1)[0][1] >= 3:
            prep.offset, prep.offset_ok = offs.most_common(1)[0][0], True
    prep.notes = _notes(prep, layer)
    prep.index = PageIndex.build({p: t for p, t in layer.items() if (t or "").strip()}, prep)
    log.info("prepared %s: %d pages%s, %d contents entries (%d located), offset %d (%s), %d scanned pages", url, n,
             " (two-page spreads)" if prep.spread else "", len(prep.contents), sum(1 for e in prep.contents if e["pdf"]),
             prep.offset, "verified" if prep.offset_ok else "unverified", prep.scanned)
    cache[url] = prep
    return prep
