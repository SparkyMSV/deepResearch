"""Chat router: Qwen decides to answer, search or start research, and summarizes finished reports."""
import asyncio
import json
import logging
import re

from app.db import insert, update
from app.local_llm import CHAT, parse_json, qwen
from app.search import search
from app.task import _post, run_task

log = logging.getLogger(__name__)

_bg: set[asyncio.Task] = set()  # strong refs: asyncio keeps only weak references to tasks
ROUTER_PROMPT = (
    "You route a chat message. Reply with JSON only: "
    '{"action":"answer|search|task","query":"..."}. '
    "answer: reply from the conversation. search: a quick web lookup (query = search query). "
    "task: a deep multi-source research job (query = the research objective)."
)
ANSWER_PROMPT = ("You are a helpful research assistant. Answer using the conversation and the evidence below. "
                 "Cite the URL of any evidence you use.")
SEARCH_PROMPT = "Reply in 2-3 sentences summarizing what these search results say about the question."
PROGRESS_PROMPT = "In one short sentence starting with 'Planning', tell the user the research is starting."
SUMMARY_PROMPT = "Summarize this research report: the bottom line, key numbers, and main risks. Be concise."


def _history(conn, conv_id: int) -> list[dict]:
    rows = conn.execute("select role, content from messages where conversation_id=? and kind='text' "
                        "order by id desc limit 10", (conv_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _words(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower()))


def _evidence(conn, conv_id: int, text: str) -> str:
    rows = conn.execute("select e.url, e.passage from evidence e join subtasks s on s.id=e.subtask_id "
                        "join tasks t on t.id=s.task_id where t.conversation_id=? and e.off_target=0",
                        (conv_id,)).fetchall()
    q = _words(text)
    scored = sorted(((len(q & _words(r["passage"])), r) for r in rows), key=lambda x: -x[0])[:20]
    # shortcut: keyword overlap, not embeddings; upgrade if answers miss relevant evidence
    return "\n".join(f"[{r['url']}] {r['passage']}" for n, r in scored if n)


async def _reply(conn, conv_id: int, system: str, extra: str = "") -> str:
    msgs = [{"role": "system", "content": system + ("\n\n" + extra if extra else "")}, *_history(conn, conv_id)]
    return (await qwen(msgs, priority=CHAT)).strip()


async def _run_and_summarize(conn, conv_id: int, task_id: int) -> None:
    try:
        report = await run_task(conn, conv_id, task_id)
        if report:  # "" means failed; the task engine already said so
            msgs = [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": report}]
            _post(conn, conv_id, task_id, (await qwen(msgs, priority=CHAT, max_tokens=500)).strip(), "text")
    except Exception:
        log.exception("task %s summary failed", task_id)


async def handle_message(conn, conv_id: int, text: str) -> None:
    if conn.execute("select count(*) from messages where conversation_id=?", (conv_id,)).fetchone()[0] == 0:
        update(conn, "conversations", conv_id, title=text[:60])
    _post(conn, conv_id, None, text, "text", role="user")
    mode, _, rest = text.partition(":")
    mode = mode.strip().lower()
    if mode in ("search", "task") and rest.strip():
        action, query = mode, rest.strip()
    else:
        raw = parse_json(await qwen([{"role": "system", "content": ROUTER_PROMPT}, *_history(conn, conv_id)],
                                    priority=CHAT, max_tokens=100))
        raw = raw if isinstance(raw, dict) else {}
        action = raw.get("action") if raw.get("action") in ("search", "task") else "answer"
        q = raw.get("query")
        query = q.strip() if isinstance(q, str) and q.strip() else text
    if action == "search":
        results = [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in await search(query, query, CHAT)]
        _post(conn, conv_id, None, json.dumps(results), "search_results")
        found = "\n".join(f"[{r['url']}] {r['title']}: {r['snippet']}" for r in results)
        _post(conn, conv_id, None, await _reply(conn, conv_id, SEARCH_PROMPT, found), "text")
    elif action == "task":
        tid = insert(conn, "tasks", conversation_id=conv_id, objective=query, status="queued")
        msgs = [{"role": "system", "content": PROGRESS_PROMPT}, {"role": "user", "content": query}]
        _post(conn, conv_id, tid, (await qwen(msgs, priority=CHAT, max_tokens=60)).strip() or "Planning…", "progress")
        t = asyncio.create_task(_run_and_summarize(conn, conv_id, tid))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    else:
        _post(conn, conv_id, None, await _reply(conn, conv_id, ANSWER_PROMPT, _evidence(conn, conv_id, text)), "text")
