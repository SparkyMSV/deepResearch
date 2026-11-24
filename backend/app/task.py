"""Task engine: cloud plan, per-sub-task search loop on Qwen, cloud check-ins, cited report."""
import asyncio
import logging
import re
from dataclasses import asdict

import openai

from app import config, events, logs
from app.cloud_llm import QuotaExhausted, cloud
from app.db import Evidence, insert, update
from app.local_llm import WORK, parse_json, qwen
from app.pdf import read_pdf
from app.logs import clip
from app.search import passages, search, url_key

log = logging.getLogger(__name__)

QUOTA_RETRY_SECONDS = 900
MAX_PDFS_PER_SUBTASK = 5  # each PDF read costs cloud "pdf" quota
CHECKIN_MAX_CHARS, REPORT_MAX_CHARS = 12_000, 60_000
_FIELDS = {"tasks": ("id", "status", "objective"),
           "subtasks": ("id", "task_id", "objective", "status", "round", "loop", "restarts")}
_task_docs: dict[int, dict[str, str]] = {}  # task_id -> {PDF url: one-line note}; offered to every sub-task

PLAN_PROMPT = (
    "Split the research objective into at most {n} independent sub-tasks, each answerable by web search. "
    "Rule: when a sub-task needs figures (financials such as revenue, EBITDA, debt or ratios, or any statistics) "
    "that an official PDF holds, such as an annual report, financial statements, a regulatory filing or a "
    "government report, set needs_document to that document with its organisation and period "
    "(e.g. \"Tata Motors annual report 2023-24\"). Rely on web pages only for figures no such document holds. "
    'Reply with JSON only: {{"subtasks":[{{"objective":"...","query":"web search query",'
    '"needs_document":"name of a specific document to find as a PDF, or empty string"}}]}}'
)
CHECKIN_PROMPT = (
    "You supervise a web research loop. Judge whether the evidence answers the objective. "
    "Any query you give must differ from the queries so far. When figures are missing, prefer a query that "
    "finds the official PDF (annual report, filing) holding them over web pages. Reply with JSON only: "
    '{"decision":"continue|sufficient|restart","query":"next query","objective":"refined objective",'
    '"off_target_loops":[loop numbers whose evidence is off-target]}'
)
REPORT_PROMPT = (
    "Write a research report in markdown answering the objective from the evidence below only. "
    "Every figure or claim must cite its source (URL, or PDF name + page) and its date. "
    "Use only figures present in the evidence; never invent or extrapolate numbers. "
    "When sources disagree on a figure, prefer the one from a PDF document (cited with a page). "
    'For evidence without a published date write "accessed <fetched_at>, publish date unknown" '
    "and flag it as lower confidence. List the gaps from incomplete sub-tasks in a final section."
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
            if prev is None:
                prev = row["status"]
                _set(conn, conv_id, table, row, status="paused_quota")
            wait = e.retry_after or QUOTA_RETRY_SECONDS
            log.warning("no cloud model free for %s; retrying in %ss", e, wait)
            await asyncio.sleep(wait)
    if prev is not None:
        _set(conn, conv_id, table, row, status=prev)
    return out


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[truncated]"


def _ask(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_task(conn, conv_id: int, task_id: int) -> str:
    logs.conv.set(conv_id)
    logs.task.set(task_id)
    t = _row(conn, "tasks", task_id)
    log.info("task start: %r", t["objective"])
    try:
        _set(conn, conv_id, "tasks", t, status="running")
        prompt = _ask(PLAN_PROMPT.format(n=config.MAX_SUBTASKS), t["objective"])
        raw = await _retry(conn, conv_id, "tasks", t, lambda: cloud("plan", prompt, conn))
        plan = (parse_json(raw) or {}).get("subtasks")
        plan = [s for s in plan if isinstance(s, dict)] if isinstance(plan, list) else []
        if not plan:
            log.warning("plan reply unusable, running the objective as one sub-task: %s", clip(raw))
        plan = plan or [{"objective": t["objective"], "query": t["objective"]}]  # bad plan JSON: one sub-task
        log.info("plan: %d sub-task(s)%s", min(len(plan), config.MAX_SUBTASKS),
                 f" (capped from {len(plan)})" if len(plan) > config.MAX_SUBTASKS else "")
        ids = []
        for s in plan[: config.MAX_SUBTASKS]:
            doc = s.get("needs_document")
            sid = insert(conn, "subtasks", task_id=task_id, status="queued",
                         objective=str(s.get("objective") or t["objective"]),
                         query=str(s.get("query") or s.get("objective") or t["objective"]),
                         needs_document=doc if isinstance(doc, str) and doc.strip() else 0)
            _set(conn, conv_id, "subtasks", _row(conn, "subtasks", sid), status="queued")
            log.info("  sub-task %s: %r | query %r | document %r", sid, s.get("objective"), s.get("query"),
                     doc or None)
            ids.append(sid)
        jobs = [asyncio.create_task(run_subtask(conn, conv_id, i)) for i in ids]
        try:
            await asyncio.gather(*jobs)
        except BaseException:
            for j in jobs:
                j.cancel()
            raise
        body = _report_input(conn, t)
        log.info("report: writing from %d chars of evidence%s", len(body),
                 f" (truncated to {REPORT_MAX_CHARS})" if len(body) > REPORT_MAX_CHARS else "")
        prompt = _ask(REPORT_PROMPT, _cap(body, REPORT_MAX_CHARS))
        report = await _retry(conn, conv_id, "tasks", t, lambda: cloud("report", prompt, conn))
        log.info("report: %d chars", len(report))
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
        _task_docs.pop(task_id, None)


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


def _qkey(q: str) -> frozenset[str]:
    """Same words in any order or case count as the same query."""
    return frozenset(re.findall(r"\w+", q.lower()))


def _fresh(q, queries: list[str]) -> str | None:
    """q stripped, or None if it's not a usable string or repeats a query already tried."""
    if not isinstance(q, str) or not q.strip():
        return None
    return None if _qkey(q) in {_qkey(x) for x in queries} else q.strip()


async def _read_doc(conn, st: dict, url: str) -> list[Evidence]:
    """Read one PDF for this sub-task's own objective; [] if capped or unreadable."""
    if st["pdf_reads"] >= MAX_PDFS_PER_SUBTASK:
        log.info("sub-task %s: PDF cap reached, skipping %s", st["id"], url)
        return []
    st["pdf_reads"] += 1
    done = st["pdf_pages"].get(url, [])
    try:
        r = await _retry(conn, st["conv_id"], "subtasks", st,
                         lambda: read_pdf(url, st["objective"], st["id"], st["loop"], conn, skip=done))
    except openai.APIError:  # cloud failures still fail the task; QuotaExhausted is retried above
        raise
    except Exception as e:  # dead link, size cap, timeout, corrupt PDF: skip this hit
        log.warning("skipping PDF %s: %r", url, e)
        st["pdf_done"].add(url)
        return []
    if r.pages:
        st["pdf_pages"][url] = done + r.pages
    else:
        st["pdf_done"].add(url)
        log.info("PDF %s: nothing more worth reading for this sub-task", url)
    docs = _task_docs.setdefault(st["task_id"], {})
    if url not in docs:
        docs[url] = r.about
        log.info("PDF %s shared with the other sub-tasks as %r", url, r.about)
    return r.evidence


async def _round(conn, st: dict, query: str, urls: set[str]) -> int:
    hits = await search(query, st["objective"], priority=WORK, seen=urls)
    added = 0
    for hit in hits:
        if hit.is_pdf:
            evs = await _read_doc(conn, st, hit.url)
        else:
            evs = [Evidence(st["id"], st["loop"], hit.url, hit.title, p, hit.published, hit.fetched_at)
                   for p in await passages(hit, st["objective"])]
        for ev in evs:
            insert(conn, "evidence", **asdict(ev))
        added += len(evs)
    log.info("round evidence: +%d passages from %d hits", added, len(hits))
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
    else:
        log.info("check-in (%d passages reviewed): decision=%s query=%r objective=%r off_target=%s",
                 len(ev), d.get("decision"), d.get("query"), clip(d.get("objective") or "", 100),
                 d.get("off_target_loops"))
    return d


async def _next(conn, st: dict, query: str, queries: list[str]) -> tuple[str, str] | None:
    """Qwen picks the next step: ("pdf", url) to read a PDF found in this task, ("search", query) for a
    new web search, or None if after 3 tries it only repeats queries already tried."""
    docs = [(u, a) for u, a in _task_docs.get(st["task_id"], {}).items() if u not in st["pdf_done"]] \
        if st["pdf_reads"] < MAX_PDFS_PER_SUBTASK else []
    user = f"Objective: {st['objective']}\nQueries tried:\n" + "\n".join(queries)
    if st["needs_document"] and not conn.execute(
            "select 1 from evidence where subtask_id=? and page is not null", (st["id"],)).fetchone():
        user += (f"\nThe figures should come from the PDF \"{st['needs_document']}\". If it is listed below, "
                 "read it; else aim the query at it (add 'pdf' or the publisher's site).")
    if docs:
        user += ("\nPDFs found in this research:\n"
                 + "\n".join(f"[{i}] {a}; " + (f"you read {len(st['pdf_pages'][u])} page(s), more may help"
                                                if u in st["pdf_pages"] else "not read by you")
                              for i, (u, a) in enumerate(docs))
                 + '\nIf one likely has the missing facts, reply {"read":N} (reads pages not read yet). Otherwise '
                 'write a new web search query that differs from every query tried: {"query":"..."}. JSON only.')
    else:
        user += ("\nWrite the next web search query to find missing facts. It must differ from every query "
                 'tried. Reply with JSON only: {"query":"..."}')
    for _ in range(3):
        d = parse_json(await qwen([{"role": "user", "content": user}], priority=WORK)) or {}
        i = d.get("read")
        i = int(i) if isinstance(i, str) and i.isdigit() else i
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(docs):
            log.info("next step: read PDF %s (%s)", docs[i][0], docs[i][1])
            return "pdf", docs[i][0]
        new = _fresh(d.get("query"), queries)
        if new:
            log.info("query rewrite: %r -> %r", query, new)
            return "search", new
        log.info("next step %s is unusable or already tried, asking again", clip(d, 120))
        user += f"\n{clip(d, 120)} is not allowed: not a listed PDF, or the query was already tried or empty."
    log.warning("next step: no new query after 3 tries")
    return None


async def run_subtask(conn, conv_id: int, subtask_id: int) -> None:
    logs.sub.set(subtask_id)
    st = _row(conn, "subtasks", subtask_id)
    st["conv_id"] = conv_id  # rides along for _round's quota retries; never written to the DB
    st["pdf_reads"] = 0  # likewise: PDF reads by this sub-task, capped at MAX_PDFS_PER_SUBTASK
    st["pdf_pages"], st["pdf_done"] = {}, set()  # pages read per PDF; PDFs with nothing more to read
    query = f"{st['needs_document']} pdf" if st["needs_document"] else st["query"]
    queries: list[str] = []
    urls: set[str] = set()  # pages already read by this sub-task; later rounds skip them
    stuck = False  # Qwen ran out of new queries: ask the supervisor now instead of searching
    doc = None  # a PDF found elsewhere in this task that Qwen chose to read next
    _set(conn, conv_id, "subtasks", st, status="running")
    while True:
        if not stuck:
            n = st["round"] + 1
            if doc:
                urls.add(url_key(doc))
                label = (f"read {'more of ' if doc in st['pdf_pages'] else ''}PDF "
                         f"{doc.split('?')[0].rsplit('/', 1)[-1] or doc}")
                log.info("round L%d·%d: reading %s instead of searching", st["loop"], n, doc)
                evs = await _read_doc(conn, st, doc)
                doc = None
                for ev in evs:
                    insert(conn, "evidence", **asdict(ev))
                log.info("round evidence: +%d facts from the PDF", len(evs))
                hits = 1
            else:
                label = query
                log.info("round L%d·%d: query %r", st["loop"], n, query)
                hits = await _round(conn, st, query, urls)
            insert(conn, "rounds", subtask_id=subtask_id, loop=st["loop"], n=n, query=label, hits=hits)
            queries.append(label)
            _set(conn, conv_id, "subtasks", st, round=n, query=label)
            events.publish(conv_id, {"type": "round", "round": {"subtask_id": subtask_id, "loop": st["loop"],
                                                                 "n": n, "query": label, "hits": hits}})
        if stuck or n % config.CHECKIN_EVERY == 0 or n >= config.ROUNDS_PER_LOOP:
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
                    if loops:
                        log.info("marked evidence from loop(s) %s off-target", loops)
                if st["restarts"] + 1 > config.MAX_RESTARTS:
                    log.info("restart cap %d reached, ending partial", config.MAX_RESTARTS)
                    _set(conn, conv_id, "subtasks", st, status="partial", restarts=st["restarts"] + 1)
                    break
                new_obj = d.get("objective") if decision == "restart" else None
                log.info("restarting loop (%s): restart %d/%d%s", "check-in asked" if decision == "restart"
                         else f"loop hit {config.ROUNDS_PER_LOOP} rounds", st["restarts"] + 1, config.MAX_RESTARTS,
                         f", new objective {new_obj!r}" if new_obj else "")
                _set(conn, conv_id, "subtasks", st, status="restarted", restarts=st["restarts"] + 1,
                     loop=st["loop"] + 1, round=0, objective=str(new_obj or st["objective"]))
                _set(conn, conv_id, "subtasks", st, status="running")
                stuck = False
                if decision == "restart" and (q := _fresh(d.get("query"), queries)):
                    query = q
                    continue
            else:
                _set(conn, conv_id, "subtasks", st, status="running")
                if stuck:
                    if not (q := _fresh(d.get("query"), queries)):
                        log.info("no new query from Qwen or the supervisor, ending partial")
                        _set(conn, conv_id, "subtasks", st, status="partial")
                        break
                    log.info("using the supervisor's query %r", q)
                    query, stuck = q, False
                    continue
        step = await _next(conn, st, query, queries)
        if step is None:
            stuck = True
        elif step[0] == "pdf":
            doc = step[1]
        else:
            query = step[1]
    count = conn.execute("select count(*) from evidence where subtask_id=? and off_target=0",
                         (subtask_id,)).fetchone()[0]
    log.info("sub-task ended %s after %d loop(s): %d usable passages", st["status"], st["loop"] + 1, count)
    line = await qwen([{"role": "user", "content":
                        f"In one short line, report that the research sub-task '{st['objective']}' ended "
                        f"as {st['status']} with {count} evidence passages."}], priority=WORK, max_tokens=60)
    _post(conn, conv_id, st["task_id"], (line.strip().splitlines() or [""])[0]
          or f"Sub-task '{st['objective']}' {st['status']}.", "progress")
