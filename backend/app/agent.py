"""Bonsai main agent: for each open field it decides once where to look and hands the work to the PDF agent
or the web agent, then reviews its own values. One sub-task at a time."""
import logging

from app import agentkit, config, events, logs, pdf_agent, web_agent
from app.ctx import date_note, new_state
from app.db import insert
from app.fields import all_fields, cite, status_line
from app.local_llm import LocalModelError, local, parse_json
from app.logs import clip

log = logging.getLogger(__name__)


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_S, _I, _L = {"type": "string"}, {"type": "integer"}, {"type": "array", "items": {"type": "integer"}}
MAIN_TOOLS = [
    _fn("explore_document", "Hand fields to the PDF agent to find in one document (by key, e.g. D1).",
        {"doc": _S, "fields": _L, "goal": _S}, ["doc", "fields", "goal"]),
    _fn("search_web", "Hand fields to the web agent to find on the web.", {"fields": _L, "goal": _S}, ["fields", "goal"]),
    _fn("mark_not_public", "Say a field is not publicly available (only after the agents looked and reported it "
        "missing).", {"field": _I, "reason": _S}, ["field", "reason"]),
    _fn("done", "Finish this part.", {"note": _S}, []),
]


def _field_lines(conn, sub) -> list[str]:
    lines, recheck = [], sub.get("recheck") or set()
    for i, f in enumerate(all_fields(conn, sub["id"]), 1):
        head = f"[{i}] {f['name']} — {f['entity'] or 'any'}, {f['period']}"
        if f["accepted"]:
            lines.append(f"{head} — done, don't work on")
            continue
        state = f"recorded {f['value']} {f['unit'] or ''}".rstrip() if f["status"] == "found" else (
            "marked not public" if f["status"] == "unavailable" else "open")
        if f["id"] in recheck:
            lines.append(f"{head} — {state} — reviewer asks to recheck: {f['hint'] or 'confirm or replace it'}")
        else:
            lines.append(head + (f" — hint: {f['hint']}" if f["hint"] else "") + f" — {state}")
    return lines


def opening(ctx, sub) -> list[dict]:
    """Fixed start of the main agent's requests (the server reuses it)."""
    docs = ctx.docs()
    doc_lines = [f"{d['key']} {d['about'] or d['name']} — covers {d['covers'] or '?'} — {d['pages'] or '?'} pages"
                 + (" — has a contents page" if d.get("contents") not in (None, "", "[]") else "")
                 for d in docs] or ["none yet"]
    eg = docs[0]["key"] if docs else None
    first = next((i for i, f in enumerate(all_fields(ctx.conn, sub["id"]), 1) if f["status"] == "open"), 1)
    text = (
        f"You lead research on: {sub['objective']}. {date_note(ctx.today)}\n"
        "You don't read pages yourself. For each open field decide once where it most likely is and hand it over:\n"
        + (f'- explore_document(doc="{eg}", fields=[{first}], goal="...") when a document likely holds it (annual '
           "reports and results for financial figures, rating rationales for ratings);\n" if eg else "")
        + f'- search_web(fields=[{first}], goal="...") when no document fits or a document didn\'t have it.\n'
        "Group related fields in one hand-off. Read each report; try the other route for fields still missing. "
        "mark_not_public only after both the web and (if any) a document were tried. Call done when every field is "
        "recorded or not public, or nothing more can be tried.\nFields:\n" + "\n".join(_field_lines(ctx.conn, sub))
        + "\nDocuments:\n" + "\n".join(doc_lines))
    return [{"role": "system", "content": text}]


def _closed(conn, sub) -> bool:
    """Every field recorded or marked not public, and none sent back by the reviewer for a recheck."""
    recheck = sub.get("recheck") or set()
    return all(f["status"] in ("found", "unavailable") and f["id"] not in recheck for f in all_fields(conn, sub["id"]))


def _nos(v) -> list[int]:
    v = v if isinstance(v, list) else [v]
    return [int(x) for x in v if isinstance(x, int) and not isinstance(x, bool) or isinstance(x, str) and x.isdigit()]


async def run_loop(ctx, sub, on_round) -> str:
    """The main agent works the sub-task by delegating; returns why it stopped."""
    sub.setdefault("recheck", set())
    sub.setdefault("reports", [])
    delegations = {"n": 0}
    rows = lambda: all_fields(ctx.conn, sub["id"])  # noqa: E731

    async def handle(name: str, a: dict) -> str:
        if name in ("explore_document", "search_web"):
            if delegations["n"] >= config.MAX_DELEGATIONS:
                return "delegation budget used for this round; call done"
            nos = [n for n in _nos(a.get("fields")) if 1 <= n <= len(rows())]
            if not nos:
                return "error: give the field numbers to look for"
            delegations["n"] += 1
            names = ", ".join(rows()[n - 1]["name"] for n in nos)
            goal = str(a.get("goal") or sub["objective"])
            if name == "explore_document":
                on_round(f"→ PDF {str(a.get('doc')).upper()}: {names}", 0)
                rep = await pdf_agent.explore(ctx, sub, str(a.get("doc") or ""), nos, goal, on_round)
            else:
                on_round(f"→ web: {names}", 0)
                rep = await web_agent.search(ctx, sub, nos, goal, on_round)
            sub["reports"].append(rep)
            return rep + "\n" + status_line(ctx.conn, sub["id"])
        if name == "mark_not_public":
            n = (_nos(a.get("field")) or [0])[0]
            if not 1 <= n <= len(rows()):
                return f"error: no field {a.get('field')}"
            f = rows()[n - 1]
            if f["status"] == "found" or f["accepted"]:
                return f"refused: [{n}] already has a value"
            need = (["the web agent"] if f["id"] not in sub.get("missing_web", set()) else []) + \
                (["a PDF agent"] if ctx.docs() and f["id"] not in sub.get("missing_pdf", set()) else [])
            if need:
                return f"refused: [{n}] hasn't been looked for by {' and '.join(need)} yet"
            ctx.set_field(f, status="unavailable", accepted=0, note=f"not public: {a.get('reason') or 'not found'}")
            on_round(f"not public: {f['name']}", 0)
            return f"marked [{n}] {f['name']} not public\n" + status_line(ctx.conn, sub["id"])
        if name == "done":
            return "ok"
        return f"error: unknown tool {name!r}"

    def compact(msgs: list[dict]) -> list[dict]:
        return opening(ctx, sub) + [{"role": "user", "content": "Reports so far (earlier messages dropped):\n"
                                     + "\n".join(sub["reports"][-6:]) + "\nContinue."}]

    if _closed(ctx.conn, sub):
        return "closed"
    msgs = opening(ctx, sub) + [{"role": "user", "content": status_line(ctx.conn, sub["id"]) + "\nBegin."}]
    why, _ = await agentkit.loop(ctx, sub, msgs, MAIN_TOOLS, handle, config.MAX_DELEGATIONS * 2 + 4, compact,
                                 ("done",), stop=lambda: _closed(ctx.conn, sub))
    return {"finished": "done"}.get(why, why)


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
            src = cite(dict(f))
            lines.append(head + f"{f['value']} {f['unit'] or ''} (reported period {f['reported_period'] or '?'}, "
                         f"{src}) quote: \"{f['quote']}\"" + (" [accepted]" if f["accepted"] else ""))
        else:
            lines.append(head + ("not public: " + (f["note"] or "") if f["status"] == "unavailable" else "not found"))
    return "\n".join(lines)


async def self_review(ctx, sub) -> str:
    """Bonsai (thinking on) checks the sub-task's values. Returns 'reopened' if anything goes back to work."""
    msgs = [{"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": f"{date_note(ctx.today)}\nTopic: {sub['objective']}\n{_review_table(ctx, sub)}"}]
    paused, wait = agentkit.pauser(ctx, sub)
    try:
        r = await local("research", msgs, think=True, max_tokens=4000, on_wait=wait)
    except LocalModelError as e:
        log.warning("self-review failed: %s", e)
        return "ok"
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
        elif act in ("reject", "retry"):  # reopened: the agents must look again before "not public" is allowed
            for key in ("missing_web", "missing_pdf"):
                sub.get(key, set()).discard(f["id"])
        if act == "reject":
            ctx.set_field(f, status="open", value=None, unit=None, reported_period=None, source_url=None,
                          source_key=None, page=None, quote=None, source_kind=None, hint=hint or f["hint"])
            reopened = True
        elif act == "retry":  # keep a found value (and its document-over-web protection); send it back for a recheck
            if f["status"] == "found":
                ctx.set_field(f, hint=hint or f["hint"])
                sub.setdefault("recheck", set()).add(f["id"])
            else:
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
    for f in all_fields(conn, subtask_id):  # say where never-found fields were looked for (report Gaps)
        if f["status"] == "open":
            where = "; ".join(sorted(st.get("looked", {}).get(f["id"], ()))) or "nothing tried"
            ctx.set_field(f, note=f"not found (looked in: {where})")
    rows = all_fields(conn, subtask_id)
    ctx.post(f"Sub-task '{st['objective']}' ended {st['status']}: {sum(bool(f['accepted']) for f in rows)}/"
             f"{len(rows)} fields checked, {ctx.pages_used} pages read in this research so far.")
