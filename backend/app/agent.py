"""Bonsai research agent: one sub-task is one tool loop over a fixed, cached opening."""
import json
import logging
from urllib.parse import urlparse

from app import config, events, logs
from app.ctx import date_note
from app.docs import page_ranges
from app.fields import all_fields, status_line
from app.ctx import new_state
from app.db import insert
from app.local_llm import local, parse_json
from app.logs import clip
from app.tools import TOOLS, run_tool

log = logging.getLogger(__name__)

MAX_CALLS_PER_TURN, REPLY_TOKENS = 3, 1000
NUDGE = "Use a tool, or call done."


def _field_lines(conn, sub) -> list[str]:
    lines = []
    for i, f in enumerate(all_fields(conn, sub["id"]), 1):
        head = f"[{i}] {f['name']} — {f['entity'] or 'any'}, {f['period']}"
        if f["accepted"]:
            lines.append(f"{head} — done, don't work on")
        else:
            state = f"recorded {f['value']} {f['unit'] or ''}".rstrip() if f["status"] == "found" else (
                "marked not public" if f["status"] == "unavailable" else "open")
            lines.append(head + (f" — hint: {f['hint']}" if f["hint"] else "") + f" — {state}")
    return lines


def opening(ctx, sub) -> list[dict]:
    """The fixed start of every request in this loop (the server reuses it, so keep it unchanged)."""
    rows = all_fields(ctx.conn, sub["id"])
    first = next((f for f in rows if f["status"] == "open"), rows[0])
    docs = ctx.docs()
    eg_doc = docs[0]["key"] if docs else "D1"
    doc_lines = [f"{d['key']} {d['about'] or d['name']} — covers {d['covers'] or '?'} — {d['pages'] or '?'} pages"
                 for d in docs] or ["none yet (search the web; PDFs you open become documents)"]
    text = (
        f"You research one part of a report: {sub['objective']}. Fill the fields below using the tools.\n"
        f"{date_note(ctx.today)}\n"
        "How to work:\n"
        f'- Prefer documents for figures: pdf_find(doc="{eg_doc}", terms=["{first["name"].lower()}"]), then '
        "pdf_page on the best pages. Use search and open_result when no document has the value.\n"
        "- After reading a page, call record_value for every field value on it, quoting the page's own words "
        "exactly (copy them, numbers as written). Only values for the field's entity and period.\n"
        "- Call mark_not_public only after reading documents and searching.\n"
        "- When every field is recorded or not public, or nothing more can be found, call done.\n"
        "Fields:\n" + "\n".join(_field_lines(ctx.conn, sub)) + "\nDocuments:\n" + "\n".join(doc_lines))
    return [{"role": "system", "content": text}]


def _notes(ctx, sub, last_text: str) -> dict:
    read = [f"{k} {page_ranges(p)}" for k, p in sub["read"].items() if p]
    return {"role": "user", "content": (
        "Progress so far (earlier messages were dropped to save space):\n" + "\n".join(_field_lines(ctx.conn, sub))
        + f"\nDocument pages already read: {'; '.join(read) or 'none'}"
        + f"\nSearches already tried: {'; '.join(sub['queries']) or 'none'}"
        + (f"\nYour last note: {last_text[:500]}" if last_text else "") + "\nContinue.")}


def _label(call: dict, result: str) -> str:
    a, name = call["args"], call["name"]
    first = result.split("\n", 1)[0]
    if name == "search":
        return f'search "{a.get("query", "")}"'
    if name == "open_result":
        url = next((w for w in first.split() if w.startswith("http")), "")
        return f"open [{a.get('n')}] {urlparse(url).netloc or first[:40]}"
    if name == "pdf_page":
        return f"{a.get('doc')} p.{a.get('page')}"
    if name == "pdf_find":
        return f"{a.get('doc')} find {', '.join(map(str, a.get('terms') or []))}"
    if name == "pdf_contents":
        return f"{a.get('doc')} contents"
    if name == "record_value":
        return first.split(" (", 1)[0].replace(f"[{a.get('field')}] ", "") if first.startswith("recorded") else first[:80]
    if name == "mark_not_public":
        return first[:80]
    return name


def _pauser(ctx, sub):
    """on_wait for local(): show a paused status and say why, once per pause; paused['on'] tells the caller
    to restore the status afterwards."""
    paused = {"on": False, "was": sub.get("status")}

    def wait(reason: str) -> None:
        paused.update(on=True, was=sub.get("status"))
        ctx.set_subtask(sub, status="paused_memory")
        ctx.post("The Mac is short of memory, so research is paused until it frees up." if reason == "memory" else
                 "The local research model (MTPLX) isn't answering; research is paused until it is running.")
    return paused, wait


def _closed(conn, sub) -> bool:
    return all(f["status"] in ("found", "unavailable") for f in all_fields(conn, sub["id"]))


async def run_loop(ctx, sub, on_round) -> str:
    """Bonsai works the sub-task with tools until done / all fields closed / step budget / stalled."""
    sub.setdefault("results", [])
    sub.setdefault("last_page", None)
    sub["done_note"] = ""
    paused, wait = _pauser(ctx, sub)

    msgs = opening(ctx, sub) + [{"role": "user", "content": status_line(ctx.conn, sub["id"]) + "\nBegin."}]
    steps = idle = tokens = sent = 0
    last_text = ""
    while True:
        if _closed(ctx.conn, sub):
            return "closed"
        if steps >= config.MAX_STEPS:
            return "steps"
        # last request's size plus what was appended since (~3 chars per token), so a turn that adds three
        # pages can't push the next request past the server's context window
        est = tokens + sum(len(str(m.get("content") or "")) for m in msgs[sent:]) // 3
        if est > config.CONTEXT_TOKENS:
            log.info("compacting the agent conversation at ~%d tokens", est)
            msgs = opening(ctx, sub) + [_notes(ctx, sub, last_text)]
        sent = len(msgs)
        r = await local("research", msgs, tools=TOOLS, max_tokens=REPLY_TOKENS, on_wait=wait)
        if paused["on"]:
            paused["on"] = False
            ctx.set_subtask(sub, status=paused["was"])
        tokens = r.prompt_tokens
        if r.content:
            last_text = r.content
        if not r.tool_calls:
            idle += 1
            if idle >= 2:
                return "stalled"
            msgs += [{"role": "assistant", "content": r.content}, {"role": "user", "content": NUDGE}]
            continue
        idle = 0
        msgs.append({"role": "assistant", "content": r.content or "", "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
            for c in r.tool_calls]})
        finished = False
        for i, c in enumerate(r.tool_calls):
            if i >= MAX_CALLS_PER_TURN:
                result = f"skipped: max {MAX_CALLS_PER_TURN} tool calls per turn"
            else:
                result, pages = await run_tool(ctx, sub, c["name"], c["args"])
                steps += 1
                on_round(_label(c, result), pages)
                finished = finished or c["name"] == "done"
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if finished:
            log.info("agent done: %s", clip(sub["done_note"], 200))
            return "done"


REVIEW_PROMPT = (
    "You check a research assistant's values before they go into a report. For each field decide: accept "
    "(the value is for the field's entity and period, and the quote supports it; or 'not public' is believable), "
    "reject (wrong or unsupported; give a hint), or retry (worth looking again; give a hint where). Reply with "
    'JSON only: {"fields":[{"field":1,"action":"accept","hint":""}]}'
)


def _review_table(ctx, sub) -> str:
    lines = []
    for i, f in enumerate(all_fields(ctx.conn, sub["id"]), 1):
        head = f"[{i}] {f['name']} — {f['entity'] or 'any'}, {f['period']}: "
        if f["status"] == "found":
            src = f"{f['source_key']} p.{f['page']}" if f["source_key"] else f["source_url"]
            lines.append(head + f"{f['value']} {f['unit'] or ''} (reported period {f['reported_period'] or '?'}, "
                         f"{src}) quote: \"{f['quote']}\"" + (" [accepted]" if f["accepted"] else ""))
        else:
            lines.append(head + ("not public: " + (f["note"] or "") if f["status"] == "unavailable" else "not found"))
    return "\n".join(lines)


async def self_review(ctx, sub) -> str:
    """Bonsai (thinking on) checks the sub-task's values. Returns 'reopened' if anything goes back to work."""
    msgs = [{"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": f"{date_note(ctx.today)}\nTopic: {sub['objective']}\n{_review_table(ctx, sub)}"}]
    paused, wait = _pauser(ctx, sub)
    r = await local("research", msgs, think=True, max_tokens=4000, on_wait=wait)
    if paused["on"]:
        ctx.set_subtask(sub, status=paused["was"])
    d = parse_json(r.content)
    items = d.get("fields") if isinstance(d, dict) else None
    if not isinstance(items, list):
        log.warning("self-review reply unusable: %s", clip(r.content, 200))
        return "ok"
    rows = all_fields(ctx.conn, sub["id"])
    reopened = False
    for a in items:
        n = a.get("field") if isinstance(a, dict) else None
        n = int(n) if isinstance(n, str) and n.isdigit() else n
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(rows) or rows[n - 1]["accepted"]:
            continue
        f, act, hint = rows[n - 1], a.get("action"), str(a.get("hint") or "").strip()
        if act == "accept" and f["status"] in ("found", "unavailable"):
            ctx.set_field(f, accepted=1)
        elif act == "reject":
            ctx.set_field(f, status="open", value=None, unit=None, reported_period=None, source_url=None,
                          source_key=None, page=None, quote=None, source_kind=None, hint=hint or f["hint"])
            reopened = True
        elif act == "retry":
            ctx.set_field(f, status="open", hint=hint or f["hint"])
            reopened = True
    log.info("self-review: %s", clip(items, 300))
    return "reopened" if reopened else "ok"


async def run_subtask(ctx, subtask_id: int) -> None:
    """Loops of Bonsai work, each followed by self-review; restarts while the review sends work back."""
    logs.sub.set(subtask_id)
    conn = ctx.conn
    st = new_state(dict(conn.execute("select * from subtasks where id=?", (subtask_id,)).fetchone()))
    ctx.set_subtask(st, status="running")

    def on_round(label: str, pages: int) -> None:
        n = st["round"] + 1
        insert(conn, "rounds", subtask_id=subtask_id, loop=st["loop"], n=n, query=label, hits=pages)
        ctx.set_subtask(st, round=n)
        events.publish(ctx.conv_id, {"type": "round", "round": {"subtask_id": subtask_id, "loop": st["loop"], "n": n,
                                                                 "query": label, "hits": pages}})

    while True:
        why = await run_loop(ctx, st, on_round)
        log.info("loop %d ended: %s", st["loop"], why)
        ctx.set_subtask(st, status="checking_in")
        res = await self_review(ctx, st)
        if all(f["accepted"] for f in all_fields(conn, subtask_id)):
            ctx.set_subtask(st, status="done")
            break
        if res == "reopened" and st["restarts"] < config.MAX_RESTARTS:
            ctx.set_subtask(st, status="running", loop=st["loop"] + 1, round=0, restarts=st["restarts"] + 1)
            continue
        ctx.set_subtask(st, status="partial")
        break
    rows = all_fields(conn, subtask_id)
    ctx.post(f"Sub-task '{st['objective']}' ended {st['status']}: {sum(bool(f['accepted']) for f in rows)}/"
             f"{len(rows)} fields checked, {ctx.pages_used} pages read in this research so far.")
