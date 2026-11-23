"""Task engine: cloud plan, per-sub-task search loop on Qwen, cloud check-ins, cited report."""
import asyncio
import logging
from dataclasses import asdict

import openai

from app import config, events
from app.cloud_llm import QuotaExhausted, cloud
from app.db import Evidence, insert, update
from app.local_llm import WORK, parse_json, qwen
from app.pdf import read_pdf
from app.search import passages, search

log = logging.getLogger(__name__)

QUOTA_RETRY_SECONDS = 900
MAX_PDFS_PER_SUBTASK = 5  # each PDF read costs cloud "pdf" quota
CHECKIN_MAX_CHARS, REPORT_MAX_CHARS = 12_000, 60_000
_FIELDS = {"tasks": ("id", "status", "objective"),
           "subtasks": ("id", "task_id", "objective", "status", "round", "loop", "restarts")}
_pdf_seen: dict[int, set[str]] = {}  # task_id -> PDF URLs already read in that task

PLAN_PROMPT = (
    "Split the research objective into at most {n} independent sub-tasks, each answerable by web search. "
    'Reply with JSON only: {{"subtasks":[{{"objective":"...","query":"web search query",'
    '"needs_document":"name of a specific document to find as a PDF, or empty string"}}]}}'
)
CHECKIN_PROMPT = (
    "You supervise a web research loop. Judge whether the evidence answers the objective. Reply with JSON only: "
    '{"decision":"continue|sufficient|restart","query":"next query","objective":"refined objective",'
    '"off_target_loops":[loop numbers whose evidence is off-target]}'
)
REPORT_PROMPT = (
    "Write a research report in markdown answering the objective from the evidence below only. "
    "Every figure or claim must cite its source (URL, or PDF name + page) and its date. "
    "Use only figures present in the evidence; never invent or extrapolate numbers. "
    'For evidence without a published date write "accessed <fetched_at>, publish date unknown" '
    "and flag it as lower confidence. List the gaps from incomplete sub-tasks in a final section."
)


def _row(conn, table: str, id: int) -> dict:
    return dict(conn.execute(f"select * from {table} where id=?", (id,)).fetchone())


def _set(conn, conv_id: int, table: str, row: dict, **cols) -> None:
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
            if prev is None:
                prev = row["status"]
                _set(conn, conv_id, table, row, status="paused_quota")
            await asyncio.sleep(e.retry_after or QUOTA_RETRY_SECONDS)
    if prev is not None:
        _set(conn, conv_id, table, row, status=prev)
    return out


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[truncated]"


def _ask(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_task(conn, conv_id: int, task_id: int) -> str:
    t = _row(conn, "tasks", task_id)
    try:
        _set(conn, conv_id, "tasks", t, status="running")
        prompt = _ask(PLAN_PROMPT.format(n=config.MAX_SUBTASKS), t["objective"])
        raw = await _retry(conn, conv_id, "tasks", t, lambda: cloud("plan", prompt, conn))
        plan = (parse_json(raw) or {}).get("subtasks")
        plan = [s for s in plan if isinstance(s, dict)] if isinstance(plan, list) else []
        plan = plan or [{"objective": t["objective"], "query": t["objective"]}]  # bad plan JSON: one sub-task
        ids = []
        for s in plan[: config.MAX_SUBTASKS]:
            doc = s.get("needs_document")
            sid = insert(conn, "subtasks", task_id=task_id, status="queued",
                         objective=str(s.get("objective") or t["objective"]),
                         query=str(s.get("query") or s.get("objective") or t["objective"]),
                         needs_document=doc if isinstance(doc, str) and doc.strip() else 0)
            _set(conn, conv_id, "subtasks", _row(conn, "subtasks", sid), status="queued")
            ids.append(sid)
        jobs = [asyncio.create_task(run_subtask(conn, conv_id, i)) for i in ids]
        try:
            await asyncio.gather(*jobs)
        except BaseException:
            for j in jobs:
                j.cancel()
            raise
        prompt = _ask(REPORT_PROMPT, _cap(_report_input(conn, t), REPORT_MAX_CHARS))
        report = await _retry(conn, conv_id, "tasks", t, lambda: cloud("report", prompt, conn))
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
        _pdf_seen.pop(task_id, None)


def _report_input(conn, t: dict) -> str:
    parts, gaps = [f"Objective: {t['objective']}"], []
    for st in conn.execute("select * from subtasks where task_id=? order by id", (t["id"],)).fetchall():
        parts.append(f"\n## Sub-task {st['id']}: {st['objective']} ({st['status']})")
        if st["status"] != "done":
            gaps.append(f"- {st['objective']} ({st['status']})")
        for e in conn.execute("select * from evidence where subtask_id=? and off_target=0 order by id",
                              (st["id"],)).fetchall():
            src = f"{e['url']} page {e['page']}" if e["page"] else e["url"]
            parts.append(f"- [{src} | {e['title']} | published: {e['published'] or 'unknown'} | "
                         f"fetched_at: {e['fetched_at']}] {e['passage']}")
    parts.append("\nGaps (incomplete sub-tasks):\n" + ("\n".join(gaps) or "none"))
    return "\n".join(parts)


async def _round(conn, st: dict, query: str, seen: set[str]) -> int:
    hits = await search(query, st["objective"], priority=WORK)
    for hit in hits:
        if hit.is_pdf:
            if hit.url in seen:
                continue
            if st["pdf_reads"] >= MAX_PDFS_PER_SUBTASK:
                log.info("sub-task %s: PDF cap reached, skipping %s", st["id"], hit.url)
                continue
            seen.add(hit.url)
            st["pdf_reads"] += 1
            try:
                evs = await _retry(conn, st["conv_id"], "subtasks", st,
                                   lambda: read_pdf(hit.url, st["objective"], st["id"], st["loop"], conn))
            except openai.APIError:  # cloud failures still fail the task; QuotaExhausted is retried above
                raise
            except Exception as e:  # dead link, size cap, timeout, corrupt PDF: skip this hit
                log.warning("skipping PDF %s: %r", hit.url, e)
                evs = []
        else:
            evs = [Evidence(st["id"], st["loop"], hit.url, hit.title, p, hit.published, hit.fetched_at)
                   for p in await passages(hit, st["objective"])]
        for ev in evs:
            insert(conn, "evidence", **asdict(ev))
    return len(hits)


async def _checkin(conn, conv_id: int, st: dict, queries: list[str]) -> dict | None:
    """The supervisor's decision, or None if the call failed or the reply wasn't JSON."""
    ev = conn.execute("select loop, url, passage from evidence where subtask_id=? and off_target=0 "
                      "order by id desc limit 40", (st["id"],)).fetchall()
    user = _cap(f"Objective: {st['objective']}\nCurrent loop: {st['loop']}\nQueries so far:\n"
                + "\n".join(queries) + "\nEvidence:\n"
                + "\n".join(f"[loop {e['loop']}] {e['url']}: {e['passage']}" for e in ev), CHECKIN_MAX_CHARS)
    try:
        raw = await _retry(conn, conv_id, "subtasks", st, lambda: cloud("checkin", _ask(CHECKIN_PROMPT, user), conn))
    except openai.APIError as e:  # end this sub-task only; siblings keep running
        log.warning("sub-task %s check-in failed: %r", st["id"], e)
        return None
    d = parse_json(raw or "")
    if d is None:
        log.warning("sub-task %s check-in reply unparseable: %r", st["id"], (raw or "")[:200])
    return d


async def _rewrite(st: dict, query: str, queries: list[str]) -> str:
    user = (f"Objective: {st['objective']}\nQueries tried:\n" + "\n".join(queries)
            + '\nWrite the next web search query to find missing facts. Reply with JSON only: {"query":"..."}')
    new = (parse_json(await qwen([{"role": "user", "content": user}], priority=WORK)) or {}).get("query")
    return new.strip() if isinstance(new, str) and new.strip() else query


async def run_subtask(conn, conv_id: int, subtask_id: int) -> None:
    st = _row(conn, "subtasks", subtask_id)
    st["conv_id"] = conv_id  # rides along for _round's quota retries; never written to the DB
    st["pdf_reads"] = 0  # likewise: PDFs read by this sub-task, capped at MAX_PDFS_PER_SUBTASK
    seen = _pdf_seen.setdefault(st["task_id"], set())
    query = f"{st['needs_document']} pdf" if st["needs_document"] else st["query"]
    queries: list[str] = []
    _set(conn, conv_id, "subtasks", st, status="running")
    while True:
        n = st["round"] + 1
        hits = await _round(conn, st, query, seen)
        insert(conn, "rounds", subtask_id=subtask_id, loop=st["loop"], n=n, query=query, hits=hits)
        queries.append(query)
        _set(conn, conv_id, "subtasks", st, round=n, query=query)
        events.publish(conv_id, {"type": "round", "round": {"subtask_id": subtask_id, "loop": st["loop"],
                                                             "n": n, "query": query, "hits": hits}})
        if n % config.CHECKIN_EVERY == 0 or n >= config.ROUNDS_PER_LOOP:
            _set(conn, conv_id, "subtasks", st, status="checking_in")
            d = await _checkin(conn, conv_id, st, queries)
            if d is None:
                _set(conn, conv_id, "subtasks", st, status="partial")
                break
            decision = d.get("decision")
            if decision == "sufficient":
                _set(conn, conv_id, "subtasks", st, status="done")
                break
            if decision == "restart" or n >= config.ROUNDS_PER_LOOP:  # a full loop counts as a restart
                if decision == "restart":
                    raw_loops = d.get("off_target_loops")
                    loops = [int(x) for x in raw_loops if isinstance(x, int) or (isinstance(x, str) and x.isdigit())
                             ] if isinstance(raw_loops, list) else []
                    conn.executemany("update evidence set off_target=1 where subtask_id=? and loop=?",
                                     [(subtask_id, x) for x in loops])
                    conn.commit()
                if st["restarts"] + 1 > config.MAX_RESTARTS:
                    _set(conn, conv_id, "subtasks", st, status="partial", restarts=st["restarts"] + 1)
                    break
                new_obj = d.get("objective") if decision == "restart" else None
                _set(conn, conv_id, "subtasks", st, status="restarted", restarts=st["restarts"] + 1,
                     loop=st["loop"] + 1, round=0, objective=str(new_obj or st["objective"]))
                _set(conn, conv_id, "subtasks", st, status="running")
                if decision == "restart" and isinstance(d.get("query"), str) and d["query"].strip():
                    query = d["query"].strip()
                    continue
            else:
                _set(conn, conv_id, "subtasks", st, status="running")
        query = await _rewrite(st, query, queries)
    count = conn.execute("select count(*) from evidence where subtask_id=? and off_target=0",
                         (subtask_id,)).fetchone()[0]
    line = await qwen([{"role": "user", "content":
                        f"In one short line, report that the research sub-task '{st['objective']}' ended "
                        f"as {st['status']} with {count} evidence passages."}], priority=WORK, max_tokens=60)
    _post(conn, conv_id, st["task_id"], (line.strip().splitlines() or [""])[0]
          or f"Sub-task '{st['objective']}' {st['status']}.", "progress")
