"""Task engine: the larger model plans a field checklist; local Qwen explores documents and the web to fill
it; the larger model reviews at check-ins and writes the report."""
import asyncio
import logging
from datetime import date

import openai

from app import checkin, config, docs, events, explore, logs
from app.cloud_llm import QuotaExhausted, cloud, on_fallback
from app.ctx import TaskCtx, new_state
from app.db import insert, update
from app.pdf import PdfStore
from app.plan import make_plan

log = logging.getLogger(__name__)

QUOTA_RETRY_SECONDS = 900
STALL_STEPS = 3  # steps on one field without filling anything before focus moves on
REPORT_MAX_CHARS = 60_000
_FIELDS = {"tasks": ("id", "status", "objective"),
           "subtasks": ("id", "task_id", "objective", "status", "round", "loop", "restarts")}
_paused: dict[int, int] = {}  # task_id -> how many of its calls are waiting for a cloud model

REPORT_PROMPT = (
    "Write a research report in markdown answering the objective from the fields and documents below only. "
    "Present key figures in tables. Every figure or claim must cite its source (document key and name with page, "
    "or URL) and its period. Use only values present below; never invent or extrapolate numbers. When sources "
    "disagree, documents outrank web pages. Mark values that are 'unreviewed' as such. End with a 'Gaps' "
    "section listing open and unavailable fields with their reasons."
)


def _row(conn, table: str, id: int) -> dict:
    return dict(conn.execute(f"select * from {table} where id=?", (id,)).fetchone())


def _set(conn, conv_id: int, table: str, row: dict, **cols) -> None:
    if "status" in cols and cols["status"] != row.get("status"):
        log.info("%s %s: %s -> %s", table[:-1], row["id"], row.get("status"), cols["status"])
    update(conn, table, row["id"], **cols)
    row.update(cols)
    kind = table[:-1]
    events.publish(conv_id, {"type": kind, kind: {k: row[k] for k in _FIELDS[table]}})


def _post(conn, conv_id: int, task_id: int | None, content: str, kind: str, role: str = "assistant") -> None:
    msg = {"role": role, "content": content, "kind": kind, "task_id": task_id}
    msg["id"] = insert(conn, "messages", conversation_id=conv_id, **msg)
    events.publish(conv_id, {"type": "message", "message": msg})


async def _retry(conn, conv_id: int, table: str, row: dict, make):
    """Await make() until it stops raising QuotaExhausted, showing paused_quota meanwhile."""
    prev = None
    while True:
        try:
            out = await make()
            break
        except QuotaExhausted as e:
            wait = e.retry_after or QUOTA_RETRY_SECONDS
            if prev is None:
                prev = row["status"]
                _set(conn, conv_id, table, row, status="paused_quota")
                task_id = row.get("task_id", row["id"])
                _paused[task_id] = _paused.get(task_id, 0) + 1
                if _paused[task_id] == 1:  # tell the user once per pause, not once per sub-task
                    _post(conn, conv_id, task_id, "Gemini and Groq are both unavailable right now, so research is "
                          "paused. " + (f"Retrying every {wait // 60 or 1} min." if e.retry_after else
                                        "Their daily limits are used up; retrying every 15 min."), "progress")
            log.warning("no cloud model free for %s; retrying in %ss", e, wait)
            await asyncio.sleep(wait)
    if prev is not None:
        _paused[row.get("task_id", row["id"])] -= 1
        _set(conn, conv_id, table, row, status=prev)
    return out


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[truncated]"


def _ask(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _closed(f: dict) -> bool:
    """Closed means a check-in accepted the value or confirmed it is not public."""
    return bool(f["accepted"])


async def run_task(conn, conv_id: int, task_id: int) -> str:
    logs.conv.set(conv_id)
    logs.task.set(task_id)
    t = _row(conn, "tasks", task_id)
    log.info("task start: %r", t["objective"])
    told: set[tuple[str, str]] = set()

    def switched(failed: list[str], used: str) -> None:  # once per task per provider pair
        names = {"gemini": "Gemini", "groq": "Groq"}
        old = names.get(config.PROVIDER[failed[0]], failed[0])
        new = names.get(config.PROVIDER[used], used)
        if old != new and (old, new) not in told:
            told.add((old, new))
            log.warning("%s failing, switched to %s (%s)", old, new, used)
            _post(conn, conv_id, task_id, f"{old} models are facing issues, so I switched to {new} ({used}).",
                  "progress")

    on_fallback.set(switched)
    ctx = TaskCtx(conn, conv_id, task_id, date.today().isoformat(), PdfStore())
    try:
        _set(conn, conv_id, "tasks", t, status="running")
        ids = await _retry(conn, conv_id, "tasks", t, lambda: make_plan(conn, task_id, t["objective"], ctx.today))
        for sid in ids:
            _set(conn, conv_id, "subtasks", _row(conn, "subtasks", sid), status="queued")
        planned = ctx.docs("planned")
        for d in planned:  # publish the list first so the UI shows what is being looked for
            ctx.set_doc(d, status="planned")
        for d in planned:
            await docs.find_document(ctx, d)
        jobs = [asyncio.create_task(run_subtask(ctx, i)) for i in ids]
        try:
            await asyncio.gather(*jobs)
        except BaseException:
            for j in jobs:
                j.cancel()
            raise
        body = _report_input(conn, t, ctx.today)
        log.info("report: writing from %d chars%s", len(body),
                 f" (truncated to {REPORT_MAX_CHARS})" if len(body) > REPORT_MAX_CHARS else "")
        prompt = _ask(REPORT_PROMPT, _cap(body, REPORT_MAX_CHARS))
        report = await _retry(conn, conv_id, "tasks", t, lambda: cloud("report", prompt, conn))
        log.info("report: %d chars (%d pages read in this research)", len(report), ctx.pages_used)
        _set(conn, conv_id, "tasks", t, status="done", report=report)
        _post(conn, conv_id, task_id, report, "report")
        return report
    except Exception as e:  # bad key, network, model errors: fail the task, never crash the server
        log.exception("task %s failed", task_id)
        _set(conn, conv_id, "tasks", t, status="failed")
        for r in conn.execute("select * from subtasks where task_id=? and status not in ('done','partial')",
                              (task_id,)).fetchall():
            _set(conn, conv_id, "subtasks", dict(r), status="failed")
        name = type(e).__name__
        text = (f"Research stopped: Gemini/Groq call failed ({name}). Check API keys in .env."
                if isinstance(e, openai.APIError) else f"Research stopped: {name}: {str(e)[:200]}.")
        _post(conn, conv_id, task_id, text, "progress")
        return ""
    finally:
        ctx.store.close()
        _paused.pop(task_id, None)


def _report_input(conn, t: dict, today: str) -> str:
    parts, gaps = [f"Objective: {t['objective']}\nToday: {today}\n\nDocuments:"], []
    for d in conn.execute("select * from documents where task_id=? order by id", (t["id"],)).fetchall():
        parts.append(f"- {d['key']} {d['name']} ({d['status']}{': ' + d['url'] if d['url'] else ''})")
    for st in conn.execute("select * from subtasks where task_id=? order by id", (t["id"],)).fetchall():
        parts.append(f"\n## Sub-task {st['id']}: {st['objective']} ({st['status']})")
        for f in conn.execute("select * from fields where subtask_id=? order by id", (st["id"],)).fetchall():
            head = f"- {f['name']} ({f['entity'] or 'any'}, {f['period']})"
            if f["value"] and f["status"] == "found":
                src = f"{f['source_key']} p.{f['page']}" if f["source_key"] else f["source_url"]
                parts.append(f"{head}: {f['value']} {f['unit'] or ''} [reported period: {f['reported_period'] or '?'}]"
                             f" source: {src} | {'accepted' if f['accepted'] else 'unreviewed'} | quote: "
                             f"\"{f['quote']}\"")
            else:
                gaps.append(f"{head}: {f['status']}{' - ' + f['note'] if f['note'] else ''}")
    parts.append("\nGaps:\n" + ("\n".join(gaps) or "none"))
    return "\n".join(parts)


def _focus(st: dict, open_: list[dict]) -> dict:
    ids = [f["id"] for f in open_]
    if st["focus"] not in ids:
        st["focus"] = ids[0]
    return next(f for f in open_ if f["id"] == st["focus"])


async def run_subtask(ctx, subtask_id: int) -> None:
    logs.sub.set(subtask_id)
    conn, conv = ctx.conn, ctx.conv_id
    st = new_state(_row(conn, "subtasks", subtask_id))
    _set(conn, conv, "subtasks", st, status="running")

    async def ask(msgs):
        return await _retry(conn, conv, "subtasks", st, lambda: cloud("checkin", msgs, conn))

    since, escalate = 0, False
    while True:
        rows = checkin.fields_of(conn, subtask_id)
        if all(_closed(f) for f in rows):
            _set(conn, conv, "subtasks", st, status="done")
            break
        open_ = [f for f in rows if f["status"] == "open"]
        need = ("confirm" if not open_ else "page budget" if not ctx.pages_left()
                else "step limit" if st["round"] >= config.ROUNDS_PER_LOOP
                else "stuck" if escalate or {f["id"] for f in open_} <= st["no_progress"]
                else "routine" if since >= config.CHECKIN_EVERY else None)
        if need:
            allow = need != "page budget" and st["restarts"] < config.MAX_RESTARTS
            log.info("check-in (%s), reopening %s", need, "allowed" if allow else "not allowed")
            _set(conn, conv, "subtasks", st, status="checking_in")
            res = await checkin.review(ctx, st, allow, ask)
            since, escalate = 0, False
            st["no_progress"].clear()
            if res is None:
                _set(conn, conv, "subtasks", st, status="partial")
                break
            if all(_closed(f) for f in checkin.fields_of(conn, subtask_id)):
                _set(conn, conv, "subtasks", st, status="done")
                break
            if res == "steered" and need == "routine":  # hints only: keep going in this loop
                res = "ok"
            if res == "steered":  # at a step limit or stuck: the hints are a new direction, if a restart is left
                res = "reopened" if allow else "blocked"
            if res == "reopened":
                log.info("check-in reopened work: loop %d, restart %d/%d", st["loop"] + 1, st["restarts"] + 1,
                         config.MAX_RESTARTS)
                _set(conn, conv, "subtasks", st, status="running", loop=st["loop"] + 1, round=0,
                     restarts=st["restarts"] + 1)
                continue
            if res == "blocked" or need != "routine":  # no new direction and nothing left to try
                _set(conn, conv, "subtasks", st, status="partial")
                break
            _set(conn, conv, "subtasks", st, status="running")
        open_ = [f for f in checkin.fields_of(conn, subtask_id) if f["status"] == "open"]
        if not open_:
            continue
        field = _focus(st, open_)
        before = {f["id"]: (f["status"], f["value"]) for f in checkin.fields_of(conn, subtask_id)}
        kind, arg = await explore.choose(ctx, st, field)
        if kind == "escalate":
            escalate = True
            continue
        n = st["round"] + 1
        pages = 0
        if kind == "doc":
            had = set(st["read"].get(arg, set()))
            pages = await docs.visit(ctx, st, arg)
            label = f"{arg} {docs.page_ranges(st['read'].get(arg, set()) - had)}"
        elif kind == "search":
            pages = await explore.web_search(ctx, st, arg, field)
            label = arg
        else:
            ctx.set_field(field, status="unavailable", accepted=0, note=f"Qwen: {arg}")
            label = f"{field['name']}: not public ({arg})"
        insert(conn, "rounds", subtask_id=subtask_id, loop=st["loop"], n=n, query=label, hits=pages)
        _set(conn, conv, "subtasks", st, round=n, query=label)
        events.publish(conv, {"type": "round", "round": {"subtask_id": subtask_id, "loop": st["loop"], "n": n,
                                                          "query": label, "hits": pages}})
        since += 1
        after = {f["id"]: (f["status"], f["value"]) for f in checkin.fields_of(conn, subtask_id)}
        if after != before:
            st["stall"][field["id"]] = 0
        else:
            st["stall"][field["id"]] = st["stall"].get(field["id"], 0) + 1
            if st["stall"][field["id"]] >= STALL_STEPS:
                st["stall"][field["id"]] = 0
                st["no_progress"].add(field["id"])
                ids = [f["id"] for f in open_]
                st["focus"] = ids[(ids.index(field["id"]) + 1) % len(ids)]
                log.info("no progress on %r after %d steps, focus moves on", field["name"], STALL_STEPS)
    rows = checkin.fields_of(conn, subtask_id)
    filled = sum(_closed(f) for f in rows)
    log.info("sub-task ended %s: %d/%d fields closed, %d pages read in research so far", st["status"], filled,
             len(rows), ctx.pages_used)
    _post(conn, conv, st["task_id"], f"Sub-task '{st['objective']}' ended {st['status']}: {filled}/{len(rows)} "
          "fields filled or confirmed not public.", "progress")
