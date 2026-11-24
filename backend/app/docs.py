"""Documents: find planned PDFs once per research (free screen, then Qwen checks page 1), and let Qwen
navigate a document page by page."""
import asyncio
import json
import logging
import re

from app.db import insert
from app.fields import read_page
from app.local_llm import WORK, parse_json, qwen
from app.logs import clip
from app.search import ddg, doc_score, key_word, mentions, name_words

log = logging.getLogger(__name__)

MAX_CANDIDATES, VISIT_PAGES, SHORT_DOC, FIRST_PAGE_MIN, MAX_SITE_SEARCHES = 3, 6, 10, 300, 2


def page_ranges(pages) -> str:
    """{1, 2, 3, 7} -> 'pp. 1–3, 7'."""
    out, ps = [], sorted(pages)
    for p in ps:
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return "pp. " + ", ".join(f"{a}–{b}" if a != b else str(a) for a, b in out) if out else "none"


def _is_pdf(url: str) -> bool:
    return "pdf" in url.lower()  # .pdf files and endpoints like GetRationalReportFilePdf?id=


async def _ask(prompt: str) -> dict | None:
    """One Qwen call with a JSON reply; one retry when it isn't JSON."""
    for attempt in range(2):
        raw = await qwen([{"role": "user", "content": prompt + ("\nReply with JSON only." if attempt else "")}],
                         priority=WORK)
        d = parse_json(raw)
        if isinstance(d, dict):
            log.info("qwen: %s", clip(d, 160))
            return d
        log.info("qwen reply unparseable: %s", clip(raw))
    return None


async def _verify(ctx, url: str, expect: dict, about_entity: bool) -> dict | None:
    try:
        n = await ctx.store.pages(url)
        text = await ctx.store.text(url, 1)
        if len(text) < FIRST_PAGE_MIN and n > 1:
            text += "\n" + await ctx.store.text(url, 2)
    except Exception as e:
        log.warning("verify %s: unreadable: %r", url, e)
        return None
    # who it's about decides; type and period are judged later from the values (strict checks rejected good PDFs)
    ask = (f"Is this PDF about {expect['issuer']}?" if about_entity else
           f"Wanted: a {expect['type']} about {expect['issuer']} (ideally {expect['period']}). Is this PDF about "
           f"{expect['issuer']} and of that kind? Another year is fine.")
    d = await _ask(f"Start of a PDF ({n} pages):\n{text[:3000]}\n\n{ask} Reply JSON only: "
                   '{"match":true|false,"about":"what it is, at most 12 words","covers":"main contents, at most 12 words"}')
    if not d or d.get("match") is not True:
        log.info("verify %s: no match", url)
        return None
    log.info("verify %s: match (%s)", url, d.get("about"))
    return {"about": str(d.get("about") or url.rsplit("/", 1)[-1]), "covers": str(d.get("covers") or ""), "pages": n}


async def verify(ctx, url: str, expect: dict, about_entity: bool = False) -> dict | None:
    """Qwen checks page 1 of a PDF against what we expect. One check per URL per research (concurrent
    callers share it). Pages read here don't count against the page budget."""
    key = f"{url}|{expect['issuer'].lower()}"  # a "no" for one company must not block the PDF for another
    if key not in ctx.inflight:
        ctx.inflight[key] = asyncio.ensure_future(_verify(ctx, url, expect, about_entity))
    return await ctx.inflight[key]


async def find_document(ctx, doc: dict) -> None:
    """Search for a planned document, screen candidates for free, verify up to 3, keep the first match."""
    words = name_words(doc["issuer"] or doc["name"])
    brand = {w} if (w := key_word(doc["issuer"] or doc["name"])) else words
    domains = json.loads(doc.get("domains") or "[]")
    # several searches: the planner's, a generic one, then the likely hosts (issuer site, exchanges); stop once
    # there are enough candidates that name the company
    queries = [doc["search"], f"{doc['issuer']} {doc['type']} {doc['period']} pdf"] + \
        [f"{doc['issuer']} {doc['type']} site:{d}" for d in domains[:MAX_SITE_SEARCHES]]
    keep, seen = [], set()
    for q in dict.fromkeys(x for x in queries if x.strip()):
        for h in await asyncio.to_thread(ddg, q):
            if not _is_pdf(h.url) or h.url in seen:
                continue
            seen.add(h.url)
            if mentions(h, brand):
                keep.append(h)
            else:
                log.info("document %s: screened out %s (no %s in URL/title/snippet)", doc["key"], h.url, sorted(brand))
        if len(keep) >= MAX_CANDIDATES:
            break
    keep.sort(key=lambda h: -doc_score(h, words, doc["period"], domains))
    expect = {"issuer": doc["issuer"] or doc["name"], "type": doc["type"] or "document", "period": doc["period"]}
    for h in keep[:MAX_CANDIDATES]:
        r = await verify(ctx, h.url, expect)
        if r:
            ctx.set_doc(doc, status="verified", url=h.url, **r)
            log.info("document %s verified: %s (%s)", doc["key"], h.url, r["about"])
            return
    ctx.set_doc(doc, status="not_found")
    log.info("document %s not found (%d candidates checked)", doc["key"], min(len(keep), MAX_CANDIDATES))


async def add_found_pdf(ctx, url: str, field: dict) -> dict | None:
    """A PDF met in web results: if it's about the field's entity and period, share it as a new document."""
    for d in ctx.docs():
        if d["url"] == url:
            return d
    r = await verify(ctx, url, {"issuer": field["entity"] or field["name"], "type": "any",
                                "period": field["period"]}, about_entity=True)
    if not r:
        return None
    for d in ctx.docs():  # another sub-task may have added it while we waited
        if d["url"] == url:
            return d
    n = ctx.conn.execute("select count(*) from documents where task_id=?", (ctx.task_id,)).fetchone()[0]
    did = insert(ctx.conn, "documents", task_id=ctx.task_id, key=f"D{n + 1}", name=r["about"],
                 issuer=field["entity"], type="any", period=field["period"], status="planned")
    row = dict(ctx.conn.execute("select * from documents where id=?", (did,)).fetchone())
    ctx.set_doc(row, status="verified", url=url, **r)
    log.info("web PDF %s shared as %s (%s)", url, row["key"], r["about"])
    return row


_GENERIC = {"total", "ratio", "value", "rate", "amount", "percentage", "percent", "figure", "level", "details"}


def search_terms(names: list[str]) -> list[str]:
    """Phrases and distinctive words of field names to look for in a document: 'Total Debt' -> ['total debt', 'debt']."""
    terms = []
    for n in names:
        words = re.findall(r"[a-z][a-z0-9&-]+", n.lower())
        for t in [" ".join(words)] + [w for w in words if len(w) >= 4 and w not in _GENERIC]:
            if t and t not in terms:
                terms.append(t)
    return terms[:12]


async def keyword_pages(ctx, doc: dict, names: list[str], read: set) -> tuple[dict, list[int]]:
    """Pages mentioning the fields (PDF text layer), and unread pages ranked: a field's full name counts 3, a
    single word 1, best per field, summed over fields; so pages covering several fields by name come first."""
    try:
        found = await ctx.store.find(doc["url"], search_terms(names))
    except Exception as e:  # no text layer, or the scan failed: contents only
        log.info("keyword scan of %s failed: %r", doc["key"], e)
        return {}, []
    score: dict[int, int] = {}
    for name in names:
        best: dict[int, int] = {}
        for t in search_terms([name]):
            for p in found.get(t, []):
                if p not in read:
                    best[p] = max(best.get(p, 0), 3 if " " in t else 1)
        for p, w in best.items():
            score[p] = score.get(p, 0) + w
    return found, sorted(score, key=lambda p: (-score[p], p))


async def _pick(ctx, sub, doc: dict, n: int, read: set) -> int | None:
    """Qwen picks where to read from the contents view; None means leave."""
    c = await ctx.store.contents(doc["url"])
    want = [f"{r['name']}" for r in ctx.conn.execute(
        "select name from fields where subtask_id=? and status='open' order by id", (sub["id"],))]
    view = ("Contents:\n" + "\n".join(f" [{i}] {t} .... {p}" for i, (t, p) in enumerate(c.entries, 1))
            if c.entries else "Opening lines:\n" + c.raw)
    found, ranked = await keyword_pages(ctx, doc, want, read)
    # the reply example names the best-ranked page: a small model copies examples literally
    best = ranked[0] if ranked else next((q for q in range(2, n + 1) if q not in read), 1)
    if found:
        view += "\nPages mentioning:\n" + "\n".join(
            f" {t}: {', '.join(map(str, [p for p in ps if p not in read][:8])) or 'all read'}" for t, ps in found.items())
    d = await _ask(f"{doc['key']} {doc['name']} ({n} pages). Looking for: {', '.join(want)}.\n{view}\n"
                   f"Read so far: {page_ranges(read)}\nReply JSON only: "
                   + (f'{{"open":1}} to read contents entry 1, {{"page":N}} for a PDF page (1-{n}), '
                      if c.entries else f'{{"page":{best}}} to read PDF page {best} (pages 1-{n}), ')
                   + '{"leave":true} if nothing here can help')
    if d and d.get("leave") is True:
        return None
    d = d or {}
    i, p = d.get("open"), d.get("page")
    if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(c.entries):
        title, printed = c.entries[i - 1]
        page, ok = await ctx.store.locate(doc["url"], title, printed)
        log.info("contents %r p.%d -> PDF page %d%s", title, printed, page, "" if ok else " (title not found)")
        return page
    if isinstance(p, int) and not isinstance(p, bool):
        return min(max(p, 1), n)
    # a muddled reply (no usable choice) is not a decision to leave: read the most-mentioned page, else the first
    log.info("navigator reply %s has no usable choice; opening the best keyword page", clip(d, 120))
    if ranked:
        return ranked[0]
    return next((q for q in range(1, n + 1) if q not in read), None)


async def visit(ctx, sub, key: str) -> int:
    """Qwen reads one document, up to 6 pages: short documents from page 1, longer ones from the contents."""
    doc = ctx.doc(key)
    read = sub["read"].setdefault(key, set())
    try:
        n = await ctx.store.pages(doc["url"])
        count, nxt = 0, None
        while count < VISIT_PAGES and ctx.pages_left():
            unread = [p for p in range(1, n + 1) if p not in read]
            if not unread:
                sub["done_docs"].add(key)
                break
            if isinstance(nxt, int):
                p = nxt
            elif n <= SHORT_DOC:
                p = unread[0]
            else:
                p = await _pick(ctx, sub, doc, n, read)
                if p is None:
                    if count == 0:  # chose this document, then read nothing: don't offer it again here
                        log.info("left %s without reading; set aside for this sub-task", key)
                        sub["done_docs"].add(key)
                    break
            if p in read or not 1 <= p <= n:  # already read: the next unread page after it
                p = next((q for q in unread if q > p), unread[0])
            text = await ctx.store.text(doc["url"], p)
            read.add(p)
            count += 1
            want = [r["name"] for r in ctx.conn.execute(
                "select name from fields where subtask_id=? and status='open' order by id", (sub["id"],))]
            _, ranked = await keyword_pages(ctx, doc, want, read)
            _, nxt = await read_page(ctx, sub, text, {"url": doc["url"], "title": doc["name"], "key": key,
                                                      "page": p, "kind": "doc", "more": ranked[:6]}, nav=True)
            if nxt == "leave":
                break
            if nxt == "contents" and n <= SHORT_DOC:
                nxt = None
        if len(read) >= n:
            sub["done_docs"].add(key)
        return count
    except Exception as e:  # download/OCR failure: this document is done for this sub-task
        log.warning("visit %s (%s) failed: %r", key, doc["url"], e)
        sub["done_docs"].add(key)
        return 0
