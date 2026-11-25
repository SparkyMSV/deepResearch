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
    index: PageIndex | None = None


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
        cands = [(e["title"], e["pdf"] + printed - e["printed"]) for e, end in runs
                 if printed >= e["printed"] and e["pdf"] + printed - e["printed"] < end]
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
    p = printed + prep.offset
    if not 1 <= p <= prep.pages:
        raise ValueError(f"page {printed} is outside the document (1-{prep.pages - prep.offset})")
    return p


def to_printed(prep: Prepared, pdf_page: int) -> int | None:
    for e, end in _runs(prep):
        if e["pdf"] <= pdf_page < end:
            return e["printed"] + pdf_page - e["pdf"]
    if prep.contents:
        return None  # front matter
    return pdf_page - prep.offset if pdf_page - prep.offset >= 1 else None


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
    prep = Prepared(pages=n, scanned=sum(1 for i in range(1, n + 1) if not (layer.get(i) or "").strip()))
    c = await store.contents(url)

    titles = [t for t, _ in c.entries]
    # the contents page lists the titles itself: never match an entry to a page that names two or more of them
    toc_pages = {p for p, t in layer.items() if sum(_starts_with(t, x) for x in titles) >= 2}

    def find(title: str, lo: int, hi: int) -> int | None:
        return next((p for p in range(max(1, lo), min(n, hi) + 1)
                     if p not in toc_pages and _starts_with(layer.get(p) or "", title)), None)

    prep.contents = [{"title": t, "printed": pr, "pdf": find(t, pr - SEARCH_BEFORE, pr + SEARCH_AFTER)}
                     for t, pr in c.entries]
    located = [e for e in prep.contents if e["pdf"]]
    if located:
        guess = Counter(e["pdf"] - e["printed"] for e in located).most_common(1)[0][0]
        for i, e in enumerate(prep.contents):  # not near its printed page: try around the common offset, then
            if e["pdf"]:                       # anywhere between the neighbouring located entries
                continue
            e["pdf"] = find(e["title"], e["printed"] + guess - SEARCH_BEFORE, e["printed"] + guess + SEARCH_BEFORE)
            if not e["pdf"]:
                prev = max((x["pdf"] for x in prep.contents[:i] if x["pdf"]), default=0)
                nxt = min((x["pdf"] for x in prep.contents[i + 1:] if x["pdf"]), default=n + 1)
                e["pdf"] = find(e["title"], prev + 1, min(nxt - 1, prev + MAX_GAP))
        last = 0
        for e in prep.contents:  # entries must run forward through the PDF; drop any that don't
            if e["pdf"] and e["pdf"] <= last:
                e["pdf"] = None
            last = e["pdf"] or last
        prep.offset, prep.offset_ok = guess, True
    else:
        # nothing located (e.g. scanned pages): keep the contents for the agent, numbers unverified
        offs = Counter()
        for p in range(1, n + 1, max(1, n // 12)):  # page numbers printed in footers/headers
            lines = [ln.strip() for ln in (layer.get(p) or "").splitlines() if ln.strip()]
            for ln in lines[:2] + lines[-3:]:
                if re.fullmatch(r"\d{1,4}", ln):
                    offs[p - int(ln)] += 1
        if offs and offs.most_common(1)[0][1] >= 3:
            prep.offset, prep.offset_ok = offs.most_common(1)[0][0], True
    prep.index = PageIndex.build({p: t for p, t in layer.items() if (t or "").strip()}, prep)
    log.info("prepared %s: %d pages, %d contents entries, offset %d (%s), %d scanned pages", url, n,
             len(prep.contents), prep.offset, "verified" if prep.offset_ok else "unverified", prep.scanned)
    cache[url] = prep
    return prep
