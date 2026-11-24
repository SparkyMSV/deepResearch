"""Qwen's moves: pick the next step for the field in focus, and run web searches it steers."""
import asyncio
import logging
import re

from app.docs import _ask, _is_pdf, add_found_pdf, page_ranges
from app.fields import open_fields, read_page
from app.logs import clip
from app.search import ddg, fetch, order_by_entity, url_key, window

log = logging.getLogger(__name__)

MAX_OPEN = 3


def qkey(q: str) -> frozenset[str]:
    """Same words in any order or case count as the same query."""
    return frozenset(re.findall(r"\w+", q.lower()))


def fresh(q, queries: list[str]) -> str | None:
    """q stripped, or None if it's not a usable string or (nearly) repeats a query already tried."""
    if not isinstance(q, str) or not q.strip():
        return None
    k = qkey(q)
    # 80%+ shared words is the same search reworded, e.g. "... yield 2024 2025" vs "... yield 2024 2025 latest"
    return None if any(len(k & qkey(x)) >= 0.8 * len(k | qkey(x)) for x in queries) else q.strip()


def _field_line(f: dict) -> str:
    return f"{f['name']} · {f['entity'] or 'any'} · {f['period']}"


async def choose(ctx, sub, field: dict) -> tuple[str, str | None]:
    """("doc", key) | ("search", query) | ("unavailable", why) | ("escalate", None)."""
    docs = [d for d in ctx.docs() if d["key"] not in sub["done_docs"]]
    others = [f["name"] for f in open_fields(ctx.conn, sub["id"]) if f["id"] != field["id"]]
    tried = [f'search "{q}"' for q in sub["queries"][-6:]] + \
        [f"{k} {page_ranges(p)}" for k, p in sub["read"].items() if p]
    opts = [f" [{d['key']}] {d['about'] or d['name']} — covers {d['covers'] or '?'} ({d['pages'] or '?'} pp) — "
            + (f"read {page_ranges(sub['read'][d['key']])}" if sub["read"].get(d["key"]) else "not read")
            for d in docs]
    prompt = (f"Today is {ctx.today}.\nField: {_field_line(field)}\n" + (f"Hint: {field['hint']}\n" if field.get("hint") else "")
              + (f"Also open: {', '.join(others)}\n" if others else "")
              + f"Tried: {'; '.join(tried) or 'nothing yet'}\nOptions:\n" + "\n".join(opts + [" [S] web search",
                                                                                         " [X] not public"])
              # the example names a real listed document: a small model copies example keys verbatim
              + "\nPick X only after reading documents and searching, if the value is truly not published."
              + "\nReply JSON only: " + (f'{{"do":"{docs[0]["key"]}"}} or ' if docs else "")
              + '{"do":"S","query":"..."} or {"do":"X","why":"..."}')
    keys = {d["key"] for d in docs}
    for _ in range(2):
        d = await _ask(prompt) or {}
        do = str(d.get("do") or "").strip().upper()
        if do in keys:
            return "doc", do
        if do == "X":
            return "unavailable", str(d.get("why") or "not found")
        if do == "S" and (q := fresh(d.get("query"), sub["queries"])):
            return "search", q
        why = "too close to a query already tried" if do == "S" else "not one of the options"
        prompt += f"\n{clip(d, 100)} is not allowed: {why}."
    for q in (field.get("search"), f"{field['entity']} {field['name']} {field['period']}".strip()):
        if (q := fresh(q, sub["queries"])):
            log.info("next step: harness fallback search %r", q)
            return "search", q
    return "escalate", None


async def web_search(ctx, sub, query: str, field: dict) -> int:
    """Search, show Qwen URL/title/snippet, open up to 3 it picks, read them. Returns pages read."""
    sub["queries"].append(query)
    hits = [h for h in await asyncio.to_thread(ddg, query) if url_key(h.url) not in sub["urls"]]
    hits = order_by_entity(hits, field["entity"])
    log.info("search %r: %d new result(s)", query, len(hits))
    if not hits:
        return 0
    listing = "\n".join(f"[{i}] {h.title}\n    {h.url}\n    {h.snippet[:200]}" for i, h in enumerate(hits, 1))
    d = await _ask(f"Looking for: {_field_line(field)}\nResults:\n{listing}\n"
                   f'Open up to {MAX_OPEN} results likely to have it. Reply JSON only: {{"open":[1,2]}}') or {}
    picks = []
    for i in d.get("open") or [] if isinstance(d.get("open"), list) else []:
        if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(hits) and i not in picks:
            picks.append(i)
    picks = picks[:MAX_OPEN] or [1]
    keywords = [w for f in open_fields(ctx.conn, sub["id"]) for w in re.findall(r"[A-Za-z]{3,}", f["name"])]
    pages = 0
    for i in picks:
        if not ctx.pages_left():
            break
        h = hits[i - 1]
        sub["urls"].add(url_key(h.url))
        got = h if _is_pdf(h.url) else await asyncio.to_thread(fetch, h)
        if got is None:
            continue
        if _is_pdf(h.url) or got.is_pdf:
            await add_found_pdf(ctx, h.url, field)
            continue
        await read_page(ctx, sub, window(got.text, keywords), {"url": h.url, "title": h.title, "key": None,
                                                              "page": None, "kind": "web"}, nav=False)
        pages += 1
    return pages
