import asyncio
from dataclasses import replace

import pytest

from app import agentkit, docs, events, web_agent
from app.ctx import TaskCtx, new_state
from app.db import Hit, connect, insert
from app.local_llm import Reply
from tests.fakes import FakeStore, Script, ScriptedQwen, call

HITS = [Hit("Nexus Select Trust results FY26", "http://a.com/r", "debt"), Hit("Other", "http://b.com/x", "")]
PAGE = "Nexus Select Trust: total borrowings were Rs 6,100 crore as of March 2026."
END = Reply("", [call("report", found="", missing="", notes="")], 10)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    monkeypatch.setattr(web_agent, "ddg", lambda q, n=None: list(HITS))
    monkeypatch.setattr(web_agent, "fetch", lambda h: replace(h, text=PAGE))
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="Debt", status="running")
    insert(conn, "fields", task_id=tid, subtask_id=sid, name="Total Debt", entity="Nexus Select Trust",
           period="FY2025-26", kind="figure", status="open")
    c = TaskCtx(conn, 1, tid, "2026-11-25", FakeStore({}))
    return c, new_state(dict(conn.execute("select * from subtasks").fetchone()))


def run(env, script, monkeypatch):
    c, sub = env
    monkeypatch.setattr(agentkit, "local", script)
    return asyncio.run(web_agent.search(c, sub, [1], "find total debt", lambda l, p: None))


def test_remark_helpful_after_record_and_history_shows_links(env, monkeypatch):
    c, _ = env
    s = Script(Reply("", [call("search", query="Nexus Select Trust total debt FY2026")], 300),
               Reply("", [call("open", n=1)], 600),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore",
                               quote="total borrowings were Rs 6,100 crore")], 600), END)
    out = run(env, s, monkeypatch)
    h = c.search_history[0]
    assert h["links"][0] == ("Nexus Select Trust results FY26", "http://a.com/r") and h["opened"] == [1]
    assert web_agent.remark(h).startswith("helpful — recorded Total Debt")
    assert "recorded: [1] Total Debt = 6,100 crore" in out
    s2 = Script(END)
    run(env, s2, monkeypatch)
    sys = s2.sent[0]["messages"][0]["content"]
    assert "http://a.com/r" in sys and "helpful — recorded Total Debt" in sys and "find total debt" in sys


def test_repeat_refused_research_wide(env, monkeypatch):
    c, _ = env
    c.search_history.append({"query": "nexus select trust total debt fy2026", "links": [], "opened": [],
                             "recorded": [], "note": ""})
    s = Script(Reply("", [call("search", query="Nexus Select Trust total debt FY2026")], 300), END)
    run(env, s, monkeypatch)
    assert "already tried" in s.sent[1]["messages"][-1]["content"]


def test_open_pdf_becomes_document_and_continues(env, monkeypatch):
    c, _ = env
    pdf = "http://n.com/ar.pdf"
    c.store.docs[pdf] = ["Nexus Select Trust annual report " * 30]
    monkeypatch.setattr(web_agent, "ddg", lambda q, n=None: [Hit("Nexus Select Trust AR", pdf, "")])
    monkeypatch.setattr(docs, "qwen", ScriptedQwen(("about Nexus", {"match": True, "about": "Nexus AR"})))
    s = Script(Reply("", [call("search", query="nexus annual report pdf")], 300),
               Reply("", [call("open", n=1)], 300), END)
    out = run(env, s, monkeypatch)
    assert "added as D1" in s.sent[2]["messages"][-1]["content"] and "new document D1" in out


def test_not_helpful_remark_and_missing(env, monkeypatch):
    c, sub = env
    s = Script(Reply("", [call("search", query="nexus debt")], 300), Reply("", [call("open", n=2)], 300), END)
    out = run(env, s, monkeypatch)
    assert web_agent.remark(c.search_history[0]) == "not helpful — opened [2], nothing recorded"
    assert "missing: [1] Total Debt" in out and sub["missing_web"]


def test_web_prompt_is_research_minded():
    import inspect
    from app import web_agent
    src = inspect.getsource(web_agent)
    assert "short queries naming the entity, field and period" not in src and "researcher" in src
