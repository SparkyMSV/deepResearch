"""Research flow: the cloud plans; Bonsai (local, MTPLX) explores each sub-task with tools and checks its own
values; consistency checks; one cloud final review; the cloud writes the report. One research at a time."""
import asyncio
import logging
from datetime import date

import openai

from app import agent, config, docs, events, final, logs
from app.cloud_llm import QuotaExhausted, cloud, on_fallback
from app.ctx import TaskCtx
from app.db import insert, update
from app.pdf import PdfStore
from app.plan import make_plan

log = logging.getLogger(__name__)

QUOTA_RETRY_SECONDS = 900
REPORT_MAX_CHARS = 60_000
LEVELS = {0: "unreviewed", 1: "checked locally", 2: "confirmed"}
_FIELDS = {"tasks": ("id", "status", "objective"),
           "subtasks": ("id", "task_id", "objective", "status", "round", "loop", "restarts")}
_paused: dict[int, int] = {}  # task_id -> how many of its calls are waiting for a cloud model
_locks: dict = {}  # event loop -> research lock (one research at a time on the one research model)

REPORT_PROMPT = (
    "Write a research report in markdown answering the objective from the fields and documents below only. "
    "Present key figures in tables. Every figure or claim must cite its source (document key and name with page, "
    "or URL) and its period, and show its review level (confirmed, checked locally, unreviewed). Use only values "
    "present below; never invent or extrapolate numbers. When sources disagree, documents outrank web pages. "
    "Repeat any 'doesn't add up' notes. End with a 'Gaps' section listing open and unavailable fields with reasons."
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


def _research_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def run_task(conn, conv_id: int, task_id: int) -> str:
    logs.conv.set(conv_id)
    logs.task.set(task_id)
    t = _row(conn, "tasks", task_id)
    lock = _research_lock()
    if lock.locked():
        _set(conn, conv_id, "tasks", t, status="queued")
        _post(conn, conv_id, task_id, "Another research is running on the local model; this one starts when it "
              "finishes.", "progress")
    async with lock:
        return await _run(conn, conv_id, t)


async def _run(conn, conv_id: int, t: dict) -> str:
    task_id = t["id"]
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
        for sid in ids:  # one at a time: the research model's cache stays warm and memory stays bounded
            await agent.run_subtask(ctx, sid)
        rows = [dict(r) for r in conn.execute("select * from fields where task_id=?", (task_id,))]
        for fid, note in final.identity_notes(rows).items():
            f = next(r for r in rows if r["id"] == fid)
            ctx.set_field(f, note=f"{f['note']}; {note}" if f["note"] else note)

        async def ask(msgs):
            return await _retry(conn, conv_id, "tasks", t, lambda: cloud("checkin", msgs, conn))

        await final.final_review(ctx, ask)
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
        # LocalModelError / LocalUnavailable are not openai.APIError, so they never read as a key problem
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
                             f" source: {src} | {LEVELS[f['accepted'] or 0]} | quote: "
                             f"\"{f['quote']}\"")
            else:
                gaps.append(f"{head}: {f['status']}{' - ' + f['note'] if f['note'] else ''}")
    parts.append("\nGaps:\n" + ("\n".join(gaps) or "none"))
    return "\n".join(parts)


