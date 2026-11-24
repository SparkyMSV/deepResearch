"""Qwen reads one page (PDF or web) and fills open fields, each value backed by a verbatim quote."""
import json
import logging

from app.db import insert
from app.local_llm import WORK, parse_json, qwen
from app.logs import clip
from app.pdf import norm

log = logging.getLogger(__name__)

PAGE_CHARS = 6000


def open_fields(conn, subtask_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "select * from fields where subtask_id=? and status='open' order by id", (subtask_id,))]


def _wanted(conn, subtask_id: int, kind: str) -> list[dict]:
    """Fields a page may fill: open ones, plus (for documents) found-from-web values a document may replace."""
    rows = [dict(r) for r in conn.execute(
        "select * from fields where subtask_id=? and accepted=0 and (status='open' or "
        "(status='found' and source_kind='web' and ?='doc')) order by id", (subtask_id, kind))]
    return rows


def _label(source: dict) -> str:
    if source["kind"] == "doc":
        return f"Page {source['page']} of {source['key']} {source['title']}"
    return f"Web page: {source['title']} ({source['url']})"


async def read_page(ctx, sub, text: str, source: dict, nav: bool) -> tuple[list[int], object]:
    """Show one page to Qwen; fill the fields it finds. Returns (filled field ids, next) where next is a page
    number, 'contents', 'leave', or None when not navigating."""
    want = _wanted(ctx.conn, sub["id"], source["kind"])
    ctx.pages_used += 1
    if not want:
        return [], ("leave" if nav else None)
    listing = "\n".join(f"[{i}] {f['name']} ({f['entity'] or 'any'}, {f['period']})" for i, f in enumerate(want, 1))
    reply = ('{"found":[{"field":1,"value":"...","unit":"...","period":"...","quote":"exact words from the page"}]'
             + (',"next":<page number>|"contents"|"leave"}' if nav else "}"))
    prompt = (f"Today is {ctx.today}. {_label(source)}. Looking for:\n{listing}\nOnly values for that entity and period. "
              f"Value without its unit; unit separately. Quote the page exactly.\n\n{text[:PAGE_CHARS].strip() or '(no text on this page)'}\n\n"
              f'Reply JSON only: {reply}. If nothing is here: {{"found":[]' + (',"next":"..."}' if nav else "}"))
    d = None
    for attempt in range(2):
        raw = await qwen([{"role": "user", "content": prompt if not attempt else prompt + "\nReply with JSON only."}],
                         priority=WORK)
        d = parse_json(raw) or _bare_list(raw)
        if isinstance(d, list):  # a bare list is the found items ([] = nothing here); keep reading on
            d = {"found": d, "next": source["page"] + 1 if nav and source["page"] else None}
        if isinstance(d, dict):
            break
        log.info("page reply unparseable: %s", clip(raw))
    if not isinstance(d, dict):
        return [], ("leave" if nav else None)
    page = norm(text)
    filled = []
    for item in d.get("found") or [] if isinstance(d.get("found"), list) else []:
        if not isinstance(item, dict):
            continue
        f = _match(item.get("field"), want)
        quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
        value = item.get("value")
        if f is None or value in (None, "") or not norm(quote) or norm(quote) not in page:
            log.info("dropped value %s (field %r): quote %s", clip(value, 40), item.get("field"),
                     f"not on the page: {clip(quote, 160)!r}" if quote else "missing")
            continue
        if f["id"] in filled:
            continue
        ctx.set_field(f, status="found", accepted=0, value=str(value), unit=str(item.get("unit") or ""),
                      reported_period=str(item.get("period") or ""), source_url=source["url"],
                      source_key=source["key"], page=source["page"], quote=norm(quote), source_kind=source["kind"])
        insert(ctx.conn, "evidence", subtask_id=sub["id"], loop=sub.get("loop") or 0, url=source["url"],
               title=source["title"], passage=norm(quote), published=None, fetched_at=ctx.today, page=source["page"])
        log.info("filled %r = %s %s (%s) from %s", f["name"], value, item.get("unit") or "", item.get("period"),
                 _label(source))
        filled.append(f["id"])
    nxt = d.get("next") if nav else None
    if nav and not (isinstance(nxt, int) and not isinstance(nxt, bool)) and nxt not in ("contents", "leave"):
        nxt = int(nxt) if isinstance(nxt, str) and nxt.isdigit() else "leave"
    return filled, nxt


def _bare_list(raw: str) -> list | None:
    """Qwen often answers 'nothing here' with a bare [] (parse_json only finds objects)."""
    try:
        d = json.loads((raw or "").replace("```json", "").replace("```", "").strip())
    except ValueError:
        return None
    return d if isinstance(d, list) else None


def _match(ref, want: list[dict]) -> dict | None:
    """A reply's field: its number in the list, or its name (case-insensitive)."""
    if isinstance(ref, str) and ref.strip().isdigit():
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        return want[ref - 1] if 1 <= ref <= len(want) else None
    if isinstance(ref, str):
        return next((f for f in want if f["name"].lower() == ref.strip().lower()), None)
    return None
