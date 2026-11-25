"""Documents: find planned PDFs once per research (free screen, then the local model checks page 1); PDFs
met during research are checked the same way and shared as new documents."""
import asyncio
import json
import logging

from app import docprep
from app.db import insert
from app.local_llm import WORK, parse_json, qwen
from app.logs import clip
from app.search import ddg, doc_score, key_word, mentions, name_words

log = logging.getLogger(__name__)

MAX_CANDIDATES, DESCRIBE_CHARS, MAX_SITE_SEARCHES = 3, 3000, 2


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


async def _ask(prompt: str, on_wait=None) -> dict | None:
    """One local-model call with a JSON reply; one retry when it isn't JSON."""
    for attempt in range(2):
        raw = await qwen([{"role": "user", "content": prompt + ("\nReply with JSON only." if attempt else "")}],
                         priority=WORK, on_wait=on_wait)
        d = parse_json(raw)
        if isinstance(d, dict):
            log.info("qwen: %s", clip(d, 160))
            return d
        log.info("qwen reply unparseable: %s", clip(raw))
    return None


async def _verify(ctx, url: str, expect: dict, about_entity: bool) -> dict | None:
    try:
        n = await ctx.store.pages(url)
        text, p = "", 1
        while p <= min(n, 4) and len(text) < DESCRIBE_CHARS:  # past a cover letter or filing intimation
            text += "\n" + await ctx.store.text(url, p)
            p += 1
    except Exception as e:
        log.warning("verify %s: unreadable: %r", url, e)
        return None
    # who it's about decides; type and period are judged later from the values (strict checks rejected good PDFs)
    ask = (f"Is this PDF about {expect['issuer']}?" if about_entity else
           f"Wanted: a {expect['type']} about {expect['issuer']} (ideally {expect['period']}). Is this PDF about "
           f"{expect['issuer']} and of that kind? Another year is fine.")
    d = await _ask(f"Start of a PDF ({n} pages):\n{text[:DESCRIBE_CHARS + 1500]}\n\n{ask} The first page may be a "
                   "cover letter or a filing intimation to a stock exchange; judge and describe the document it "
                   "encloses. Reply JSON only: "
                   '{"match":true|false,"about":"what it is, at most 12 words","covers":"main contents, at most 12 words"}',
                   on_wait=lambda reason: ctx.post(
                       "The Mac is short of memory, so research is paused until it frees up." if reason == "memory"
                       else "The local research model (MTPLX) isn't answering; research is paused until it is running."))
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
            await _prepare(ctx, doc)
            log.info("document %s verified: %s (%s)", doc["key"], h.url, r["about"])
            return
    ctx.set_doc(doc, status="not_found")
    log.info("document %s not found (%d candidates checked)", doc["key"], min(len(keep), MAX_CANDIDATES))


async def _prepare(ctx, doc: dict) -> None:
    """Contents, page offsets and page index for the PDF agent; stored on the document row."""
    try:
        p = await docprep.prepare(ctx.store, doc["url"])
    except Exception as e:  # preparation is a help, not a requirement
        log.warning("preparing %s failed: %r", doc["key"], e)
        return
    notes = ([f"{p.scanned} scanned pages not indexed"] if p.scanned else []) + \
        ([] if p.offset_ok else ["page offset unverified"])
    ctx.set_doc(doc, pages=p.pages, offset_ok=int(p.offset_ok), notes="; ".join(notes),
                contents=json.dumps([{"title": e["title"], "printed": e["printed"]} for e in p.contents]))


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
    await _prepare(ctx, row)
    log.info("web PDF %s shared as %s (%s)", url, row["key"], r["about"])
    return row
