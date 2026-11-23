import asyncio
import json

from app import chat, events, task
from app.db import Hit, connect, insert
from app.local_llm import CHAT


class Env:
    def __init__(self, monkeypatch, router='{"action":"answer"}', run_task=None, hits=None):
        self.conn = connect(":memory:")
        self.conv = insert(self.conn, "conversations", title=None)
        self.events, self.calls, self.searches, self.router = [], [], [], router
        self.hits = [Hit("T", "http://u", "snip")] if hits is None else hits
        monkeypatch.setattr(events, "publish", lambda c, e: self.events.append(e))
        monkeypatch.setattr(chat, "qwen", self.qwen)
        monkeypatch.setattr(chat, "search", self.search)
        if run_task:
            monkeypatch.setattr(chat, "run_task", run_task)

    async def qwen(self, messages, priority=None, max_tokens=800):
        assert priority == CHAT
        self.calls.append(messages)
        if messages[0]["content"] == chat.ROUTER_PROMPT:
            return self.router
        return "qwen reply"

    async def search(self, query, objective, priority=None):
        self.searches.append((query, priority))
        return self.hits

    def msgs(self):
        return [dict(r) for r in self.conn.execute("select * from messages order by id")]


def test_prefix_forces_search(monkeypatch):
    env = Env(monkeypatch, router="should not be used")
    asyncio.run(chat.handle_message(env.conn, env.conv, "search: x"))
    assert env.searches == [("x", CHAT)]
    assert all(c[0]["content"] != chat.ROUTER_PROMPT for c in env.calls)
    m = env.msgs()
    assert [x["kind"] for x in m] == ["text", "search_results", "text"]
    assert json.loads(m[1]["content"]) == [{"title": "T", "url": "http://u", "snippet": "snip"}]
    assert any(e["type"] == "message" for e in env.events)


def test_bad_router_json_answers(monkeypatch):
    env = Env(monkeypatch, router="not json")
    asyncio.run(chat.handle_message(env.conn, env.conv, "hello"))
    assert env.searches == []
    m = env.msgs()
    assert (m[-1]["role"], m[-1]["content"]) == ("assistant", "qwen reply")


def test_answer_includes_evidence_with_url(monkeypatch):
    env = Env(monkeypatch)
    t = insert(env.conn, "tasks", conversation_id=env.conv, objective="o", status="done")
    s = insert(env.conn, "subtasks", task_id=t, status="done")
    insert(env.conn, "evidence", subtask_id=s, url="http://ev", passage="solar capacity grew", off_target=0)
    asyncio.run(chat.handle_message(env.conn, env.conv, "solar capacity?"))
    assert "http://ev" in json.dumps(env.calls[-1])


def test_task_returns_immediately(monkeypatch):
    async def go():
        gate = asyncio.Event()

        async def fake(conn, conv_id, task_id):
            await gate.wait()
            return ""

        env = Env(monkeypatch, run_task=fake)
        await chat.handle_message(env.conn, env.conv, "task: study x")
        assert not gate.is_set() and chat._bg
        assert env.msgs()[-1]["kind"] == "progress"
        gate.set()
        await asyncio.gather(*chat._bg)
        assert env.msgs()[-1]["kind"] == "progress"  # failure: no summary

    asyncio.run(go())


def test_summary_after_report(monkeypatch):
    async def go():
        async def fake(conn, conv_id, task_id):
            task._post(conn, conv_id, task_id, "# Report", "report")
            return "# Report"

        env = Env(monkeypatch, run_task=fake)
        await chat.handle_message(env.conn, env.conv, "task: study x")
        await asyncio.gather(*chat._bg)
        m = env.msgs()
        assert (m[-2]["kind"], m[-1]["kind"], m[-1]["content"]) == ("report", "text", "qwen reply")

    asyncio.run(go())


def test_title_set_from_first_message(monkeypatch):
    env = Env(monkeypatch)
    asyncio.run(chat.handle_message(env.conn, env.conv, "a" * 100))
    asyncio.run(chat.handle_message(env.conn, env.conv, "second"))
    title = env.conn.execute("select title from conversations").fetchone()[0]
    assert title == "a" * 60


def test_answer_includes_latest_report(monkeypatch):
    env = Env(monkeypatch)
    insert(env.conn, "messages", conversation_id=env.conv, role="assistant", kind="report",
           content="RISK-NUMBER-42 " + "x" * 7000)
    asyncio.run(chat.handle_message(env.conn, env.conv, "what was the risk number?"))
    sys = env.calls[-1][0]["content"]
    assert "RISK-NUMBER-42" in sys and len(sys) < 7000


def test_no_evidence_answer_has_no_citation_instruction(monkeypatch):
    env = Env(monkeypatch)
    asyncio.run(chat.handle_message(env.conn, env.conv, "hello"))
    sys = env.calls[-1][0]["content"]
    assert sys == chat.GENERAL_PROMPT and "Cite" not in sys and "search:" in sys


def test_empty_search_skips_qwen(monkeypatch):
    env = Env(monkeypatch, hits=[])
    asyncio.run(chat.handle_message(env.conn, env.conv, "search: nothing"))
    assert env.calls == []
    m = env.msgs()
    assert (m[-1]["kind"], m[-1]["content"]) == ("text", "No usable results found for that search.")
