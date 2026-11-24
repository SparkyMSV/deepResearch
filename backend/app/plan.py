"""Plan: the larger model turns an objective into documents to find and a checklist of fields to fill."""
import json
import logging

from app import config
from app.cloud_llm import cloud
from app.ctx import date_note
from app.db import insert
from app.local_llm import parse_json

log = logging.getLogger(__name__)

MAX_FIELDS_PER_SUBTASK, MAX_FIELDS, MAX_DOCUMENTS = 8, 40, 6

PLAN_PROMPT = (
    "{note} Plan research for the objective as a checklist a small model will fill. "
    "Split it into at most {n} sub-tasks. Each sub-task lists fields: specific figures (number + unit) or short "
    "facts, each for a named entity and period. Prefer the latest complete period available as of today. "
    "For figures prefer official documents (annual report, audited results, filings, rating rationales, "
    "government statistics) and list them as documents with the sites likely to host them. A document's issuer "
    "is the company it is about (for a rating rationale, the rated company, not the agency). Qualitative "
    "questions become short-fact fields. Don't plan fields that need private data. Reply with JSON only: "
    '{{"documents":[{{"key":"D1","name":"...","issuer":"...","type":"annual report","period":"FY2025-26",'
    '"search":"web search query","domains":["example.com"]}}],"subtasks":[{{"objective":"...","fields":[{{'
    '"name":"Gross NPA %","entity":"...","period":"FY2025-26","kind":"figure|fact","sources":["D1"],'
    '"search":"web search query","hint":"where this value usually appears"}}]}}]}}'
)


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


async def make_plan(conn, task_id: int, objective: str, today: str) -> list[int]:
    """Insert planned documents, sub-tasks and fields for a task; return the sub-task ids."""
    msgs = [{"role": "system", "content": PLAN_PROMPT.format(note=date_note(today), n=config.MAX_SUBTASKS)},
            {"role": "user", "content": objective}]
    data = None
    for attempt in range(2):
        raw = await cloud("plan", msgs, conn)
        data = parse_json(raw)
        if isinstance(data, dict) and isinstance(data.get("subtasks"), list):
            break
        log.warning("plan reply unusable (attempt %d): %r", attempt + 1, (raw or "")[:200])
        msgs = msgs + [{"role": "assistant", "content": raw or ""},
                       {"role": "user", "content": "Your reply was not valid JSON. Reply with JSON only."}]
        data = None
    docs = [d for d in (data or {}).get("documents") or [] if isinstance(d, dict) and _s(d.get("name"))]
    subs = [s for s in (data or {}).get("subtasks") or [] if isinstance(s, dict)]
    if not subs:
        subs = [{"objective": objective, "fields": [{"name": objective, "kind": "fact"}]}]
    if len(docs) > MAX_DOCUMENTS or len(subs) > config.MAX_SUBTASKS:
        log.info("plan capped: %d→%d documents, %d→%d sub-tasks", len(docs), min(len(docs), MAX_DOCUMENTS),
                 len(subs), min(len(subs), config.MAX_SUBTASKS))
    keys = {}  # planner key -> D1..Dn
    for i, d in enumerate(docs[:MAX_DOCUMENTS], 1):
        keys[_s(d.get("key")) or f"#{i}"] = f"D{i}"
        domains = [x for x in d.get("domains") or [] if isinstance(x, str)] if isinstance(d.get("domains"), list) else []
        insert(conn, "documents", task_id=task_id, key=f"D{i}", name=_s(d["name"]), issuer=_s(d.get("issuer")),
               type=_s(d.get("type")), period=_s(d.get("period")) or "latest", status="planned",
               search=_s(d.get("search")) or f"{_s(d['name'])} pdf", domains=json.dumps(domains))
        log.info("  document D%d: %r (%s, %s)", i, _s(d["name"]), _s(d.get("issuer")), _s(d.get("period")))
    ids, total = [], 0
    for s in subs[: config.MAX_SUBTASKS]:
        if total >= MAX_FIELDS:  # a sub-task with no fields would end "done" with nothing done
            log.info("plan: field cap %d reached, dropping remaining sub-tasks", MAX_FIELDS)
            break
        obj = _s(s.get("objective")) or objective
        sid = insert(conn, "subtasks", task_id=task_id, objective=obj, query=obj, status="queued")
        ids.append(sid)
        fields = [f for f in s.get("fields") or [] if isinstance(f, dict) and _s(f.get("name"))] \
            if isinstance(s.get("fields"), list) else []
        if not fields:  # a sub-task with no fields still gets something to fill
            fields = [{"name": obj, "kind": "fact"}]
        for f in fields[:MAX_FIELDS_PER_SUBTASK]:
            if total >= MAX_FIELDS:
                break
            src = [keys[k] for k in f.get("sources") or [] if isinstance(k, str) and k in keys] \
                if isinstance(f.get("sources"), list) else []
            insert(conn, "fields", task_id=task_id, subtask_id=sid, name=_s(f["name"]), entity=_s(f.get("entity")),
                   period=_s(f.get("period")) or "latest",
                   kind=f.get("kind") if f.get("kind") in ("figure", "fact") else "fact",
                   sources=json.dumps(src), search=_s(f.get("search")), hint=_s(f.get("hint")), status="open")
            total += 1
        log.info("  sub-task %s: %r, %d field(s)", sid, obj, min(len(fields), MAX_FIELDS_PER_SUBTASK))
    return ids
