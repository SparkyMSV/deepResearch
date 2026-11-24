"""The research agent's tools. Guardrails live here, so the model can only do sensible things: no repeat
searches or pages, exact quotes, documents before web pages, a page budget."""
import asyncio
import logging
import re

from app.docs import _is_pdf, add_found_pdf
from app.fields import _why_not, all_fields, record, status_line
from app.pdf import norm
from app.logs import clip
from app.search import ddg, fetch, order_by_entity, url_key, window

log = logging.getLogger(__name__)

SHOW_RESULTS, PAGE_CHARS, CONTENTS_CHARS, RECENT_PAGES = 8, 6000, 2500, 6


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_S, _I = {"type": "string"}, {"type": "integer"}
TOOLS = [
    _fn("search", "Web search. Returns numbered results (title, URL, snippet). One short query per field.",
        {"query": _S}, ["query"]),
    _fn("open_result", "Open result n of the last search and read it.", {"n": _I}, ["n"]),
    _fn("pdf_contents", "Contents page of a document (by key, e.g. D1).", {"doc": _S}, ["doc"]),
    _fn("pdf_find", "Pages of a document that mention each term.", {"doc": _S, "terms": {"type": "array", "items": _S}},
        ["doc", "terms"]),
    _fn("pdf_page", "Read one page of a document.", {"doc": _S, "page": _I}, ["doc", "page"]),
    _fn("record_value", "Record a field's value from a page you just read, with an exact quote from it. For a table "
        "row, quote the row label and the value joined by ' ... '.",
        {"field": _I, "value": _S, "unit": _S, "period": _S, "quote": _S}, ["field", "value", "quote"]),
    _fn("mark_not_public", "Say a field's value is not publicly available, after searching and reading documents.",
        {"field": _I, "reason": _S}, ["field", "reason"]),
    _fn("done", "Finish: every field is recorded or not public, or nothing more can be found.", {"note": _S}, ["note"]),
]


def qkey(q: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", q.lower()))


def fresh(q, queries: list[str]) -> str | None:
    """q stripped, or None if it's empty or shares 80%+ of its words with a query already tried."""
    if not isinstance(q, str) or not q.strip():
        return None
    k = qkey(q)
    return None if any(len(k & qkey(x)) >= 0.8 * len(k | qkey(x)) for x in queries) else q.strip()


def _remember(sub, source: dict) -> None:
    """Pages a value may be quoted from: the last few read (newest last)."""
    sub["recent"] = (sub.get("recent") or [])[-(RECENT_PAGES - 1):] + [source]
    sub["last_page"] = source


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    return int(v) if isinstance(v, str) and v.strip().isdigit() else None


def _focus(ctx, sub) -> dict:
    rows = all_fields(ctx.conn, sub["id"])
    return next((f for f in rows if f["status"] == "open"), rows[0])


def _doc(ctx, key) -> dict | None:
    d = ctx.doc(str(key or "").strip().upper())
    return d if d and d["status"] == "verified" else None


async def _search(ctx, sub, query=None):
    q = fresh(query, sub["queries"])
    if q is None:
        return f"already tried a very similar search: {query!r}. Try different words, or read a document.", 0
    sub["queries"].append(q)
    hits = [h for h in await asyncio.to_thread(ddg, q, 20) if url_key(h.url) not in sub["urls"]]
    hits = order_by_entity(hits, _focus(ctx, sub)["entity"])[:SHOW_RESULTS]
    sub["results"] = hits
    if not hits:
        return f'no new results for "{q}"', 0
    return "\n".join(f"[{i}] {h.title} — {h.url}\n    {h.snippet[:200]}" for i, h in enumerate(hits, 1)), 0


async def _open_result(ctx, sub, n=None):
    i, hits = _int(n), sub.get("results") or []
    if i is None or not 1 <= i <= len(hits):
        return f"error: no result {n}; the last search had {len(hits)}", 0
    if not ctx.pages_left():
        return BUDGET, 0
    h = hits[i - 1]
    sub["urls"].add(url_key(h.url))
    got = None if _is_pdf(h.url) else await asyncio.to_thread(fetch, h)
    if _is_pdf(h.url) or (got and got.is_pdf):
        d = await add_found_pdf(ctx, h.url, _focus(ctx, sub))
        if not d:
            return f"That PDF is not about {_focus(ctx, sub)['entity']}; skipped.", 0
        return (f"Added as {d['key']}: {d['about']} ({d['pages']} pages). Use pdf_find / pdf_contents / pdf_page "
                f"on {d['key']}."), 0
    if got is None:
        return f"could not read {h.url}", 0
    names = [f["name"] for f in all_fields(ctx.conn, sub["id"]) if f["status"] == "open"]
    text = window(got.text, [w for n_ in names for w in re.findall(r"[A-Za-z]{3,}", n_)], PAGE_CHARS)
    ctx.pages_used += 1
    _remember(sub, {"url": h.url, "title": h.title, "key": None, "page": None, "kind": "web", "text": text})
    return f"[{h.title}] {h.url}\n{text}", 1


async def _pdf_contents(ctx, sub, doc=None):
    d = _doc(ctx, doc)
    if not d:
        return f"error: no verified document {doc!r}", 0
    c = await ctx.store.contents(d["url"])
    if c.entries:
        return f"{d['key']} contents (printed page numbers; PDF pages may be a few higher):\n" + \
            "\n".join(f"  {t} .... {p}" for t, p in c.entries), 0
    return f"{d['key']} has no contents page. Opening lines:\n{c.raw[:CONTENTS_CHARS]}\nUse pdf_find to locate pages.", 0


async def _pdf_find(ctx, sub, doc=None, terms=None):
    d = _doc(ctx, doc)
    if not d or not isinstance(terms, list) or not terms:
        return "error: pdf_find needs a verified document key and a list of terms", 0
    found = await ctx.store.find(d["url"], [str(t).lower() for t in terms if str(t).strip()][:8])
    read = sub["read"].get(d["key"], set())
    lines = [f"  {t}: " + ", ".join(f"{p}{' (read)' if p in read else ''}" for p in ps[:15]) for t, ps in found.items()]
    return f"{d['key']} pages mentioning:\n" + ("\n".join(lines) or "  nothing (or no text layer: use pdf_contents)"), 0


async def _pdf_page(ctx, sub, doc=None, page=None):
    d, p = _doc(ctx, doc), _int(page)
    if not d or p is None:
        return "error: pdf_page needs a verified document key and a page number", 0
    n = await ctx.store.pages(d["url"])
    if not 1 <= p <= n:
        return f"error: {d['key']} has pages 1-{n}", 0
    read = sub["read"].setdefault(d["key"], set())
    if p in read:
        return f"already read {d['key']} p.{p}; pick another page", 0
    if not ctx.pages_left():
        return BUDGET, 0
    text = (await ctx.store.text(d["url"], p))[:PAGE_CHARS]
    read.add(p)
    ctx.pages_used += 1
    _remember(sub, {"url": d["url"], "title": d["name"], "key": d["key"], "page": p, "kind": "doc", "text": text})
    return f"{d['key']} page {p} of {n}:\n{text.strip() or '(no text on this page)'}", 1


async def _record_value(ctx, sub, field=None, value=None, unit="", period="", quote=""):
    recent = sub.get("recent") or []
    if not recent:
        return "rejected: read a page first (pdf_page or open_result); values must be quoted from it", 0
    # the model often reads two or three pages in one turn, then records from any of them
    src = next((p for p in reversed(recent) if _why_not(str(value or ""), str(quote or ""), norm(p["text"])) is None),
               recent[-1])
    out = record(ctx, sub, _int(field), str(value or ""), str(unit or ""), str(period or ""), str(quote or ""), src)
    if out.startswith("recorded"):  # a fresh value answers the reviewer's recheck
        rows, i = all_fields(ctx.conn, sub["id"]), _int(field)
        (sub.get("recheck") or set()).discard(rows[i - 1]["id"])
    return out, 0


async def _mark_not_public(ctx, sub, field=None, reason=""):
    rows, i = all_fields(ctx.conn, sub["id"]), _int(field)
    if i is None or not 1 <= i <= len(rows):
        return f"error: no field {field}", 0
    f = rows[i - 1]
    if f["status"] == "found" or f["accepted"]:
        return f"rejected: [{i}] already has a value", 0
    ctx.set_field(f, status="unavailable", accepted=0, note=f"Bonsai: {reason or 'not found'}")
    return f"marked [{i}] {f['name']} not public", 0


async def _done(ctx, sub, note=""):
    sub["done_note"] = str(note or "")
    return "ok, finishing", 0


BUDGET = "Page budget used up; record what you have and call done."
_TOOLS = {"search": _search, "open_result": _open_result, "pdf_contents": _pdf_contents, "pdf_find": _pdf_find,
          "pdf_page": _pdf_page, "record_value": _record_value, "mark_not_public": _mark_not_public, "done": _done}


async def run_tool(ctx, sub, name: str, args: dict) -> tuple[str, int]:
    """Run one tool call; returns (result text ending with the field status line, pages read)."""
    fn = _TOOLS.get(name)
    if fn is None:
        text, pages = f"error: unknown tool {name!r}", 0
    elif "_raw" in args:
        text, pages = f"error: arguments for {name} were not valid JSON", 0
    else:
        try:
            text, pages = await fn(ctx, sub, **args)
        except TypeError as e:
            text, pages = f"error: bad arguments for {name}: {e}", 0
        except Exception as e:  # download/OCR/network failure: report it, keep the loop going
            log.warning("tool %s failed: %r", name, e)
            text, pages = f"error: {name} failed ({type(e).__name__})", 0
    log.info("tool %s %s -> %s", name, clip(args, 120), clip(text, 160))
    return f"{text}\n{status_line(ctx.conn, sub['id'])}", pages
