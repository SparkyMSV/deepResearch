"""Background document preparation: contents, per-section page offsets and a page index (BM25), so the PDF
agent can work in printed page numbers and find anchors before reading. No model involved."""
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from app.pdf import norm

log = logging.getLogger(__name__)

SEARCH_BEFORE, SEARCH_AFTER, LOCATED_MIN = 5, 25, 1
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
            cands = [c for c in cands if section.lower() in c[0].lower()]
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

    async def page_text(p: int) -> str:
        return layer.get(p) or ""

    for title, printed in c.entries:
        e = {"title": title, "printed": printed, "pdf": None}
        for p in range(max(1, printed - SEARCH_BEFORE), min(n, printed + SEARCH_AFTER) + 1):
            if _starts_with(await page_text(p), title):
                e["pdf"] = p
                break
        prep.contents.append(e)
    located = [e for e in prep.contents if e["pdf"]]
    if located:
        offs = Counter(e["pdf"] - e["printed"] for e in located)
        guess = offs.most_common(1)[0][0]
        for e in prep.contents:  # entries whose title wasn't found: assume the common offset
            e["pdf"] = e["pdf"] or (e["printed"] + guess if 1 <= e["printed"] + guess <= n else None)
        prep.offset, prep.offset_ok = guess, len(located) >= LOCATED_MIN
    else:
        prep.contents = []
        offs = Counter()
        for p in range(1, n + 1, max(1, n // 12)):  # page numbers printed in footers/headers
            lines = [ln.strip() for ln in (await page_text(p)).splitlines() if ln.strip()]
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
