"""Web agent: searches with the research's search history in view (every query, every link it returned, and
whether it helped), opens results, records values. PDFs it meets become documents for the main agent."""
import asyncio
import logging
import re

from app import agentkit, config
from app.ctx import date_note
from app.docs import _is_pdf, add_found_pdf
from app.fields import _why_not, all_fields, record
from app.logs import clip
from app.pdf import norm
from app.search import ddg, fetch, order_by_entity, window
from app.tools import fresh

log = logging.getLogger(__name__)

SHOW_RESULTS, PAGE_CHARS, HISTORY_FULL = 8, 6000, 10


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_S, _I = {"type": "string"}, {"type": "integer"}
WEB_TOOLS = [
    _fn("search", "Web search (several engines). One short query; note: a one-line remark on the previous search.",
        {"query": _S, "note": _S}, ["query"]),
    _fn("open", "Open link n of the latest search and read it. A PDF is handed to the main agent as a document.",
        {"n": _I}, ["n"]),
    _fn("record_value", "Record a field's value from a page you opened, quoting its own words. For a table row, "
        "quote the row label and the value joined by ' ... '.",
        {"field": _I, "value": _S, "unit": _S, "period": _S, "quote": _S}, ["field", "value", "quote"]),
    _fn("report", "Finish: what you found, which fields are missing, and any lead (e.g. a document to read).",
        {"found": _S, "missing": _S, "notes": _S}, ["notes"]),
]


def remark(h: dict) -> str:
    """Whether a past search helped, from what actually happened (plus the agent's own note)."""
    if h["recorded"]:
        text = "helpful — recorded " + ", ".join(f"{name} from [{n}]" for name, n in h["recorded"])
    elif h["opened"]:
        text = f"not helpful — opened {h['opened']}, nothing recorded"
    else:
        text = "no results opened"
    return text + (f"; note: {h['note']}" if h.get("note") else "")


def _history(ctx) -> str:
    hs = ctx.search_history
    if not hs:
        return "none yet"
    old, recent = hs[:-HISTORY_FULL], hs[-HISTORY_FULL:]
    lines = [f'- "{h["query"]}": {remark(h)}' for h in old]
    for h in recent:
        links = "  ".join(f"[{i}] {t[:60]} — {u}" for i, (t, u) in enumerate(h["links"], 1))
        lines.append(f'query: "{h["query"]}"\n  links: {links or "none"}\n  remark: {remark(h)}')
    return "\n".join(lines)


def _int(v):
    if isinstance(v, bool):
        return None
    return v if isinstance(v, int) else (int(v) if isinstance(v, str) and v.strip().isdigit() else None)


async def search(ctx, sub, field_nos: list[int], goal: str, on_round) -> str:
    """Run the web agent for the given field numbers; returns its report for the main agent."""
    rows = all_fields(ctx.conn, sub["id"])
    fields = [(n, rows[n - 1]) for n in field_nos if isinstance(n, int) and 1 <= n <= len(rows)]
    if not fields:
        return "error: no valid fields to look for"
    entity = fields[0][1]["entity"]
    opened: list[dict] = []  # pages opened in this call: {"url","title","text","entry","n"}
    recorded, new_docs, mine = [], [], []
    current: dict = {}

    async def handle(name: str, a: dict) -> str:
        if name == "search":
            q = fresh(a.get("query"), [h["query"] for h in ctx.search_history])
            if ctx.search_history and a.get("note"):
                ctx.search_history[-1]["note"] = clip(a["note"], 120)
            if q is None:
                return f"already tried a very similar search: {a.get('query')!r}. Use different words."
            hits = order_by_entity(await asyncio.to_thread(ddg, q, 20), entity)[:SHOW_RESULTS]
            h = {"query": q, "links": [(x.title, x.url) for x in hits], "hits": hits, "opened": [], "recorded": [],
                 "note": ""}
            ctx.search_history.append(h)
            mine.append(h)
            current["h"] = h
            on_round(f'search "{q}"', 0)
            return "\n".join(f"[{i}] {x.title} — {x.url}\n    {x.snippet[:200]}" for i, x in enumerate(hits, 1)) \
                or f'no results for "{q}"'
        if name == "open":
            h, n = current.get("h"), _int(a.get("n"))
            if not h or n is None or not 1 <= n <= len(h["hits"]):
                return f"error: no link {a.get('n')} in the latest search"
            if not ctx.pages_left():
                return "Page budget used up; record what you have and call report."
            x = h["hits"][n - 1]
            h["opened"].append(n)
            got = None if _is_pdf(x.url) else await asyncio.to_thread(fetch, x)
            if _is_pdf(x.url) or (got and got.is_pdf):
                d = await add_found_pdf(ctx, x.url, fields[0][1])
                if not d:
                    return f"That PDF is not about {entity}; skipped."
                new_docs.append(f"{d['key']} ({d['about']})")
                on_round(f"new document {d['key']}", 0)
                return f"This is a PDF: added as {d['key']} ({d['about']}). The main agent can explore it; carry on."
            if got is None:
                return f"could not read {x.url}"
            words = [w for _, f in fields for w in re.findall(r"[A-Za-z]{3,}", f["name"])]
            text = window(got.text, words, PAGE_CHARS)
            opened.append({"url": x.url, "title": x.title, "text": text, "entry": h, "n": n})
            ctx.pages_used += 1
            on_round(f"open [{n}] {x.url.split('/')[2] if '//' in x.url else x.url}", 1)
            return f"[{x.title}] {x.url}\n{text}"
        if name == "record_value":
            if not opened:
                return "rejected: open a page first; values must be quoted from it"
            v, q = str(a.get("value") or ""), str(a.get("quote") or "")
            page = next((p for p in reversed(opened) if _why_not(v, q, norm(p["text"])) is None), opened[-1])
            src = {"url": page["url"], "title": page["title"], "key": None, "page": None, "kind": "web",
                   "text": page["text"]}
            n = _int(a.get("field"))
            out = record(ctx, sub, n, v, str(a.get("unit") or ""), str(a.get("period") or ""), q, src)
            if out.startswith("recorded"):
                f = all_fields(ctx.conn, sub["id"])[n - 1]
                (sub.get("recheck") or set()).discard(f["id"])
                page["entry"]["recorded"].append((f["name"], page["n"]))
                recorded.append(f"[{n}] {f['name']} = {f['value']} {f['unit'] or ''}".rstrip())
                on_round(out.split(" (", 1)[0].replace(f"[{n}] ", ""), 0)
            else:
                on_round(out[:80], 0)
            return out
        if name == "report":
            return "ok"
        return f"error: unknown tool {name!r}"

    objective = "\n".join(f"[{n}] {f['name']} — {f['entity'] or 'any'}, {f['period']}"
                          + (f" — hint: {f['hint']}" if f["hint"] else "") for n, f in fields)
    system = (f"You search the web for specific figures. {date_note(ctx.today)}\nObjective: {goal}\nFields:\n"
              f"{objective}\nSearch history of this research (don't repeat queries; learn from the remarks):\n"
              f"{_history(ctx)}\nHow to work: short queries naming the entity, field and period; open the most "
              "promising links; record_value with a quote copied from the page; call report when done or stuck.")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]

    def compact(m: list[dict]) -> list[dict]:
        done = "; ".join(f'"{h["query"]}" ({remark(h)})' for h in mine)
        return [m[0], {"role": "user", "content": f"Earlier steps were dropped to save space. Searches so far: {done}. "
                       f"Recorded: {', '.join(recorded) or 'nothing'}. Continue."}]

    why, args = await agentkit.loop(ctx, sub, msgs, WEB_TOOLS, handle, config.WEB_STEPS, compact, ("report",))
    now = all_fields(ctx.conn, sub["id"])
    missing = [(n, f) for n, f in fields if now[n - 1]["status"] != "found"]
    sub.setdefault("missing_web", set()).update(f["id"] for _, f in missing)
    for _, f in fields:
        sub.setdefault("looked", {}).setdefault(f["id"], set()).add(f"{len(mine)} web searches")
    notes = str((args or {}).get("notes") or "").strip()
    ended = {"finished": "", "steps": " (stopped: step budget used)", "stalled": " (stopped: no tool use)",
             "error": " (stopped: model error)"}[why]
    report = (f"Web agent{ended}: recorded: {', '.join(recorded) or 'nothing'}; missing: "
              + (", ".join(f"[{n}] {f['name']}" for n, f in missing) or "none")
              + (f"; new documents: {', '.join('new document ' + d for d in new_docs)}" if new_docs else "")
              + (f"; notes: {notes}" if notes else "") + f"; searches: {len(mine)}")
    log.info("%s", report)
    return report
