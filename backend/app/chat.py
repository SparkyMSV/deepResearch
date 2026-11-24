"""Chat router: Qwen decides to answer, search or start research, and summarizes finished reports."""
import asyncio
import json
import logging
import re

from app import events, logs
from app.db import insert, update
from app.local_llm import CHAT, parse_json, qwen
from app.logs import clip
from app.search import search
from app.task import _post, run_task

log = logging.getLogger(__name__)

_bg: set[asyncio.Task] = set()  # strong refs: asyncio keeps only weak references to tasks
ROUTER_PROMPT = (
    "You route the user's last chat message. Reply with JSON only: "
    '{"action":"answer|search|task","query":"..."}. '
    "answer: reply from the conversation. search: a quick web lookup. "
    "task: a deep multi-source research job. "
    "query: a complete, standalone web search query (or research objective) for the last message. "
    "Follow-ups refer back to earlier turns, so carry over their topic, country, time period and "
    "entities, and drop chat words like 'search:' or 'how about'. Never add years or facts the user did "
    "not mention; reuse the conversation's own time period. Example: after a search for Indian "
    "insurance companies' revenue in 2026, 'how about for health insurance only' -> "
    "'Indian health insurance companies revenue 2026'."
)
ANSWER_PROMPT = ("You are a helpful research assistant. Answer using the conversation and the evidence below. "
                 "Cite the URL of any evidence you use.")
GENERAL_PROMPT = ("Answer from general knowledge, concisely. Do not cite sources or URLs. If the question needs "
                  "current facts, say you haven't looked it up and suggest prefixing with search:.")
NO_RESULTS = "No usable results found for that search."
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


def _report(conn, conv_id: int) -> str:
    r = conn.execute("select content from messages where conversation_id=? and kind='report' "
                     "order by id desc limit 1", (conv_id,)).fetchone()
    return f"Latest research report (truncated):\n{r['content'][:6000]}" if r else ""


def _status(conv_id: int, phase: str | None) -> None:
    events.publish(conv_id, {"type": "status", "text": phase})


async def _reply(conn, conv_id: int, system: str, extra: str = "") -> str:
    msgs = [{"role": "system", "content": system + ("\n\n" + extra if extra else "")}, *_history(conn, conv_id)]
    _status(conv_id, "writing")
    on_token = lambda t: events.publish(conv_id, {"type": "delta", "text": t})
    out = (await qwen(msgs, priority=CHAT, on_token=on_token)).strip()
    log.info("assistant: %s", clip(out, 300))
    return out


async def _run_and_summarize(conn, conv_id: int, task_id: int) -> None:
    try:
        report = await run_task(conn, conv_id, task_id)
        if report:  # "" means failed; the task engine already said so
            log.info("summarizing report for chat")
            msgs = [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": report}]
            _post(conn, conv_id, task_id, (await qwen(msgs, priority=CHAT, max_tokens=500)).strip(), "text")
    except Exception:
        log.exception("task %s summary failed", task_id)


async def handle_message(conn, conv_id: int, text: str) -> None:
    logs.conv.set(conv_id)
    log.info("user: %s", clip(text, 300))
    try:
        await _handle(conn, conv_id, text)
    finally:  # always clear the busy indicator, even on errors
        _status(conv_id, None)


async def _handle(conn, conv_id: int, text: str) -> None:
    if conn.execute("select count(*) from messages where conversation_id=?", (conv_id,)).fetchone()[0] == 0:
        update(conn, "conversations", conv_id, title=text[:60])
    _post(conn, conv_id, None, text, "text", role="user")
    _status(conv_id, "thinking")
    mode, _, rest = text.partition(":")
    mode = mode.strip().lower()
    forced = mode in ("search", "task") and rest.strip()
    history = _history(conn, conv_id)
    if forced and len(history) <= 1:  # first message: nothing to resolve, use it as typed
        action, query = mode, rest.strip()
    else:
        # the router also frames follow-ups into standalone queries, so forced modes use it too
        # user turns only: Qwen's own replies leak stray details (years, entities) into the query
        asked = [m for m in history if m["role"] == "user"]
        raw = parse_json(await qwen([{"role": "system", "content": ROUTER_PROMPT}, *asked],
                                    priority=CHAT, max_tokens=100))
        raw = raw if isinstance(raw, dict) else {}
        action = raw.get("action") if raw.get("action") in ("search", "task") else "answer"
        q = raw.get("query")
        query = q.strip() if isinstance(q, str) and q.strip() else (rest.strip() if forced else text)
        if forced:
            action = mode
    log.info("route: %s%s, query %r", action, " (forced by prefix)" if forced else "", query)
    if action == "search":
        _status(conv_id, "searching")
        results = [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in await search(query, query, CHAT)]
        if not results:  # nothing to summarize: don't let Qwen invent sources
            log.info("search found nothing usable")
            _post(conn, conv_id, None, NO_RESULTS, "text")
            return
        _post(conn, conv_id, None, json.dumps(results), "search_results")
        found = "\n".join(f"[{r['url']}] {r['title']}: {r['snippet']}" for r in results)
        log.info("showing %d results; summarizing", len(results))
        _post(conn, conv_id, None, await _reply(conn, conv_id, SEARCH_PROMPT, found), "text")
    elif action == "task":
        tid = insert(conn, "tasks", conversation_id=conv_id, objective=query, status="queued")
        log.info("task %s created; running in the background", tid)
        msgs = [{"role": "system", "content": PROGRESS_PROMPT}, {"role": "user", "content": query}]
        _post(conn, conv_id, tid, (await qwen(msgs, priority=CHAT, max_tokens=60)).strip() or "Planning…", "progress")
        t = asyncio.create_task(_run_and_summarize(conn, conv_id, tid), name=f"conv-{conv_id}")
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    else:
        context = "\n\n".join(x for x in (_report(conn, conv_id), _evidence(conn, conv_id, text)) if x)
        prompt = ANSWER_PROMPT if context else GENERAL_PROMPT
        log.info("answering from %s", f"{len(context)} chars of saved report/evidence" if context
                 else "general knowledge (no evidence)")
        _post(conn, conv_id, None, await _reply(conn, conv_id, prompt, context), "text")
