"""Check-ins: the larger model reviews a sub-task's fields table and steers Qwen."""
import json
import logging

import openai

from app.db import insert
from app.docs import find_document, page_ranges
from app.local_llm import parse_json
from app.logs import clip
from app.plan import MAX_FIELDS, MAX_FIELDS_PER_SUBTASK

log = logging.getLogger(__name__)

MAX_ADD, TABLE_MAX_CHARS = 3, 12_000

CHECKIN_PROMPT = (
    "You review a small model's research. For each field decide: accept (value fits the entity and period and "
    "the quote supports it), reject (wrong or unsupported; it will be searched again), unavailable (not publicly "
    "available; say why in note) or retry (keep looking; give a hint on where to look and/or a new search). "
    "You may add up to 3 missing fields, or ask for one more document to find; its issuer is the company it is "
    "about (for a rating rationale, the rated company, not the agency). Reply with JSON only: "
    '{"fields":[{"field":1,"action":"accept|reject|unavailable|retry","hint":"...","search":"...","note":"..."}],'
    '"add_fields":[{"name":"...","entity":"...","period":"...","kind":"figure|fact","search":"...","hint":"..."}],'
    '"find_document":{"name":"...","issuer":"...","type":"...","period":"...","search":"..."}}'
)


def fields_of(conn, subtask_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("select * from fields where subtask_id=? order by id", (subtask_id,))]


def _row_for(a: dict, rows: list[dict]) -> dict | None:
    """The field a review item means: its number in the table, else its name (and entity, if given)."""
    n = a.get("field")
    n = int(n) if isinstance(n, str) and n.strip().isdigit() else n
    if isinstance(n, int) and not isinstance(n, bool):
        return rows[n - 1] if 1 <= n <= len(rows) else None
    name, ent = str(a.get("name") or "").strip().lower(), str(a.get("entity") or "").strip().lower()
    return next((f for f in rows if f["name"].lower() == name and (not ent or (f["entity"] or "").lower() == ent)),
                None)


def _table(ctx, sub) -> str:
    rows = []
    for i, f in enumerate(fields_of(ctx.conn, sub["id"]), 1):
        state = "accepted" if f["accepted"] else f["status"]
        page = f" p.{f['page']}" if f["page"] else ""
        src = f"{f['source_key'] or f['source_url'] or ''}{page}"
        rows.append(f"[{i}] {f['name']} | {f['entity'] or 'any'} | {f['period']} | {state} | "
                    + (f"{f['value']} {f['unit'] or ''} (reported period: {f['reported_period'] or '?'}) | {src} | "
                       f"quote: \"{f['quote']}\"" if f["value"] else "no value")
                    + (f" | note: {f['note']}" if f["note"] else ""))
    read = [f"{k} {page_ranges(p)}" for k, p in sub["read"].items() if p]
    return ("Fields:\n" + "\n".join(rows) + "\nDocuments read: " + ("; ".join(read) or "none")
            + "\nSearches: " + ("; ".join(sub["queries"]) or "none"))


async def review(ctx, sub, allow_reopen: bool, ask) -> str | None:
    """Apply the larger model's review. ask(messages) -> reply text (the caller handles quota pauses).
    Returns 'reopened', 'blocked' (it wanted to reopen work but the restart cap stops it), 'steered' (only new
    hints/searches for fields still open), 'ok', or None
    when the call failed or the reply wasn't JSON."""
    msgs = [{"role": "system", "content": CHECKIN_PROMPT},
            {"role": "user", "content": f"Today is {ctx.today}. Objective: {sub['objective']}\n{_table(ctx, sub)}"[:TABLE_MAX_CHARS]}]
    try:
        raw = await ask(msgs)
    except openai.APIError as e:  # end this sub-task only; siblings keep running
        log.warning("check-in failed: %r", e)
        return None
    d = parse_json(raw or "")
    if not isinstance(d, dict):
        log.warning("check-in reply unparseable: %s", clip(raw))
        return None
    log.info("check-in: %s", clip(d, 300))
    rows = fields_of(ctx.conn, sub["id"])
    reopened = blocked = steered = False

    def reopen(what: str) -> bool:
        nonlocal reopened, blocked
        if allow_reopen:
            reopened = True
        else:
            blocked = True
            log.info("check-in: restart cap reached, dropping %s", what)
        return allow_reopen

    for a in d.get("fields") or [] if isinstance(d.get("fields"), list) else []:
        f = _row_for(a, rows) if isinstance(a, dict) else None
        if f is None or f["accepted"]:  # nothing replaces or reopens an accepted value
            continue
        act = a.get("action")
        if act == "accept" and f["status"] == "found":
            ctx.set_field(f, accepted=1)
        elif act == "unavailable":
            ctx.set_field(f, status="unavailable", accepted=1, note=str(a.get("note") or "not publicly available"))
        elif act == "reject" and reopen(f"reject {f['name']}"):
            ctx.set_field(f, status="open", accepted=0, value=None, unit=None, reported_period=None, source_url=None,
                          source_key=None, page=None, quote=None, source_kind=None, note=a.get("note"))
        elif act == "retry" and f["status"] == "open":  # steering only: new hint/search for Qwen
            cols = {k: a[k].strip() for k in ("hint", "search") if isinstance(a.get(k), str) and a[k].strip()}
            ctx.set_field(f, **cols)
            steered = steered or bool(cols)
        elif act == "retry" and reopen(f"retry {f['name']}"):
            cols = {"status": "open", "accepted": 0} if f["status"] == "unavailable" else {}
            for k in ("hint", "search"):
                if isinstance(a.get(k), str) and a[k].strip():
                    cols[k] = a[k].strip()
            if f["status"] == "found" and not f["accepted"]:
                cols.update(status="open")  # keep the value; a better source may replace it
            ctx.set_field(f, **cols)
    adds = [x for x in d.get("add_fields") or [] if isinstance(x, dict) and str(x.get("name") or "").strip()] \
        if isinstance(d.get("add_fields"), list) else []
    if adds and reopen(f"{len(adds)} new field(s)"):
        have = len(rows)
        total = ctx.conn.execute("select count(*) from fields where task_id=?", (ctx.task_id,)).fetchone()[0]
        for x in adds[:MAX_ADD]:
            if have >= MAX_FIELDS_PER_SUBTASK or total >= MAX_FIELDS:
                break
            fid = insert(ctx.conn, "fields", task_id=ctx.task_id, subtask_id=sub["id"], name=str(x["name"]).strip(),
                         entity=str(x.get("entity") or ""), period=str(x.get("period") or "latest"),
                         kind=x.get("kind") if x.get("kind") in ("figure", "fact") else "fact", sources="[]",
                         search=str(x.get("search") or ""), hint=str(x.get("hint") or ""), status="planned")
            ctx.set_field(dict(ctx.conn.execute("select * from fields where id=?", (fid,)).fetchone()), status="open")
            have, total = have + 1, total + 1
    fd = d.get("find_document")
    if isinstance(fd, dict) and str(fd.get("name") or "").strip() and reopen("find_document"):
        n = ctx.conn.execute("select count(*) from documents where task_id=?", (ctx.task_id,)).fetchone()[0]
        did = insert(ctx.conn, "documents", task_id=ctx.task_id, key=f"D{n + 1}", name=str(fd["name"]),
                     issuer=str(fd.get("issuer") or ""), type=str(fd.get("type") or ""),
                     period=str(fd.get("period") or "latest"), status="planned",
                     search=str(fd.get("search") or f"{fd['name']} pdf"), domains=json.dumps([]))
        await find_document(ctx, dict(ctx.conn.execute("select * from documents where id=?", (did,)).fetchone()))
    return "reopened" if reopened else "blocked" if blocked else "steered" if steered else "ok"
