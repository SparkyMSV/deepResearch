import asyncio
import json

import pytest

from app import agent, config, events
from app.ctx import TaskCtx, new_state
from app.db import connect, insert
from app.local_llm import Reply
from tests.fakes import FakeStore

AR = "http://navi.com/ar.pdf"
PAGES = ["Cover", "Contents", "Asset quality: Gross NPA was 2.1% and Net NPA was 0.9% in FY2025-26."] + [f"p{i}" for i in range(4, 13)]


def call(name, i=0, **args):
    return {"id": f"c{name}{i}", "name": name, "args": args}


class Script:
    """Fake agent.local: returns scripted Replies, records the messages it was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    async def __call__(self, role, messages, tools=None, max_tokens=800, think=False, on_token=None, on_wait=None,
                       max_wait=None):
        self.sent.append({"role": role, "messages": json.loads(json.dumps(messages)), "think": think, "tools": tools})
        r = self.replies.pop(0) if self.replies else Reply("", [call("done", note="x")], 100)
        if callable(r):
            r = r(on_wait)
        return r


@pytest.fixture
def env(monkeypatch):
    sent = []
    monkeypatch.setattr(events, "publish", lambda cid, e: sent.append(e))
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="Asset quality", status="running")
    for n in ("Gross NPA %", "Net NPA %"):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=n, entity="Navi Finserv", period="FY2025-26",
               kind="figure", status="open", hint="asset quality table")
    insert(conn, "documents", task_id=tid, key="D1", name="Navi AR", issuer="Navi Finserv", status="verified",
           url=AR, about="Navi AR FY26", covers="financials", pages=len(PAGES))
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({AR: PAGES}))
    sub = new_state(dict(conn.execute("select * from subtasks").fetchone()))
    rounds = []
    return c, sub, rounds, sent


def loop(env, script, monkeypatch):
    c, sub, rounds, _ = env
    monkeypatch.setattr(agent, "local", script)
    return asyncio.run(agent.run_loop(c, sub, lambda label, pages: rounds.append((label, pages))))


QUOTE = "Gross NPA was 2.1% and Net NPA was 0.9%"


def test_tool_calls_run_and_results_appended(env, monkeypatch):
    s = Script(Reply("", [call("pdf_page", doc="D1", page=3)], 900),
               Reply("", [call("record_value", 1, field=1, value="2.1", unit="%", quote=QUOTE),
                          call("record_value", 2, field=2, value="0.9", unit="%", quote=QUOTE)], 1500))
    assert loop(env, s, monkeypatch) == "closed"
    _, _, rounds, _ = env
    assert rounds == [("D1 p.3", 1), ("recorded Gross NPA % = 2.1 %", 0), ("recorded Net NPA % = 0.9 %", 0)]
    second = s.sent[1]["messages"]
    assert second[-1]["role"] == "tool" and "Gross NPA was 2.1%" in second[-1]["content"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "pdf_page"


def test_max_three_calls_per_turn(env, monkeypatch):
    s = Script(Reply("", [call("pdf_page", i, doc="D1", page=i) for i in (1, 2, 4, 5)], 900))
    loop(env, s, monkeypatch)
    _, sub, rounds, _ = env
    assert len([r for r in rounds if r[0].startswith("D1 p.")]) == 3 and 5 not in sub["read"]["D1"]
    assert "skipped: max 3" in s.sent[1]["messages"][-1]["content"]


def test_ends_on_step_budget(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 2)
    s = Script(*[Reply("", [call("pdf_page", i, doc="D1", page=i)], 900) for i in (4, 5, 6)])
    assert loop(env, s, monkeypatch) == "steps"


def test_done_ends_loop(env, monkeypatch):
    assert loop(env, Script(Reply("", [call("done", note="nothing more")], 900)), monkeypatch) == "done"


def test_text_only_reply_nudged_then_stalled(env, monkeypatch):
    s = Script(Reply("thinking aloud", [], 900), Reply("still talking", [], 900))
    assert loop(env, s, monkeypatch) == "stalled"
    assert s.sent[1]["messages"][-1] == {"role": "user", "content": "Use a tool, or call done."}


def test_compaction_keeps_state_and_refusals(env, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TOKENS", 1000)
    s = Script(Reply("", [call("pdf_page", doc="D1", page=3)], 1500),
               Reply("", [call("pdf_page", 1, doc="D1", page=3)], 900))
    loop(env, s, monkeypatch)
    compacted = s.sent[1]["messages"]
    assert len(compacted) == 2 and compacted[0]["role"] == "system"
    assert "D1 pp. 3" in compacted[1]["content"]
    _, _, rounds, _ = env
    assert any(r[0] == "D1 p.3" for r in rounds)  # the repeat was refused, not re-read


def test_opening_marks_closed_fields_and_lists_documents(env):
    c, sub, _, _ = env
    c.conn.execute("update fields set status='found', value='2.1', accepted=1 where name='Gross NPA %'")
    sys = agent.opening(c, sub)[0]["content"]
    assert "[1] Gross NPA %" in sys and "done, don't work on" in sys and "D1 Navi AR FY26" in sys
    assert "Today is 2026-11-24" in sys and 'pdf_find(doc="D1"' in sys


def test_memory_pause_sets_status_and_posts_once(env, monkeypatch):
    c, sub, _, sent = env

    def paused(on_wait):
        on_wait("memory")
        return Reply("", [call("done", note="x")], 100)

    loop(env, Script(paused), monkeypatch)
    statuses = [e["subtask"]["status"] for e in sent if e["type"] == "subtask"]
    assert statuses == ["paused_memory", "running"]
    notes = [e["message"]["content"] for e in sent if e["type"] == "message"]
    assert len(notes) == 1 and "memory" in notes[0].lower()


def test_big_tool_results_trigger_compaction_before_the_next_request(env, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TOKENS", 3000)
    c, _, _, _ = env
    c.store.docs[AR] = ["x" * 6000] * 12
    s = Script(Reply("", [call("pdf_page", i, doc="D1", page=i) for i in (4, 5)], 1000),
               Reply("", [call("done", note="x")], 900))
    loop(env, s, monkeypatch)
    assert len(s.sent[1]["messages"]) == 2  # compacted: 1000 tokens + ~4000 from two pages > 3000
