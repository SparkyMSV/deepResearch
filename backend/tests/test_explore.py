import asyncio
from dataclasses import replace

import pytest

from app import docs, events, explore, fields
from app.ctx import TaskCtx, new_state
from app.db import Hit, connect, insert
from tests.fakes import FakeStore, ScriptedQwen


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="s", status="running")
    insert(conn, "fields", task_id=tid, subtask_id=sid, name="Gross NPA %", entity="Navi Finserv", period="FY2025-26",
           kind="figure", status="open", search="Navi Finserv gross NPA March 2026", hint="asset quality table")
    for k, name in (("D1", "Navi AR"), ("D2", "CARE rationale")):
        insert(conn, "documents", task_id=tid, key=k, name=name, status="verified", url=f"http://x/{k}.pdf",
               about=name, covers="ratings", pages=6)
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({}))
    sub = new_state(dict(conn.execute("select * from subtasks").fetchone()))
    field = dict(conn.execute("select * from fields").fetchone())
    return c, sub, field


def qwen(monkeypatch, *rules):
    q = ScriptedQwen(*rules)
    for m in (docs, fields):  # explore asks Qwen through docs._ask
        monkeypatch.setattr(m, "qwen", q)
    return q


def test_choose_doc_option_lists_only_unfinished_docs(env, monkeypatch):
    c, sub, field = env
    sub["done_docs"].add("D1")
    q = qwen(monkeypatch, ("Options:", {"do": "d2"}))
    assert asyncio.run(explore.choose(c, sub, field)) == ("doc", "D2")
    assert "[D1]" not in q.prompts[0] and "[D2] CARE rationale" in q.prompts[0] and "asset quality table" in q.prompts[0]


def test_choose_refuses_repeat_then_falls_back_to_planner_search(env, monkeypatch):
    c, sub, field = env
    sub["queries"].append("navi finserv npa")
    q = qwen(monkeypatch, ("Options:", {"do": "S", "query": "Navi Finserv NPA"}))
    assert asyncio.run(explore.choose(c, sub, field)) == ("search", "Navi Finserv gross NPA March 2026")
    assert len(q.prompts) == 2 and "too close" in q.prompts[1]


def test_choose_escalates_when_queue_exhausted(env, monkeypatch):
    c, sub, field = env
    sub["queries"] += ["Navi Finserv gross NPA March 2026", "Navi Finserv Gross NPA % FY2025-26"]
    qwen(monkeypatch, ("Options:", "junk"))
    assert asyncio.run(explore.choose(c, sub, field)) == ("escalate", None)


def test_choose_not_public(env, monkeypatch):
    c, sub, field = env
    qwen(monkeypatch, ("Options:", {"do": "X", "why": "not disclosed"}))
    assert asyncio.run(explore.choose(c, sub, field)) == ("unavailable", "not disclosed")


def hits():
    return [Hit(f"Navi result {i}", f"http://site{i}.com/a", f"snippet {i}") for i in range(4)]


def test_web_search_opens_chosen_results_and_skips_seen(env, monkeypatch):
    c, sub, field = env
    sub["urls"].add("http://site0.com/a")
    monkeypatch.setattr(explore, "ddg", lambda q: hits())
    fetched = []
    monkeypatch.setattr(explore, "fetch", lambda h: fetched.append(h.url) or replace(h, text="GNPA was 2.1%"))
    q = qwen(monkeypatch, ("Results:", {"open": [1, 3]}), ("Looking for", {"found": []}))
    assert asyncio.run(explore.web_search(c, sub, "navi gnpa", field)) == 2
    assert fetched == ["http://site1.com/a", "http://site3.com/a"] and "site0" not in q.prompts[0]
    assert sub["queries"] == ["navi gnpa"] and c.pages_used == 2


def test_web_search_pdf_becomes_document(env, monkeypatch):
    c, sub, field = env
    url = "http://care.com/navi.pdf"
    c.store = FakeStore({url: ["CARE rationale Navi Finserv FY26"]})
    monkeypatch.setattr(explore, "ddg", lambda q: [Hit("Navi Finserv CARE", url, "rating")])
    qwen(monkeypatch, ("Results:", {"open": [1]}), ("about Navi Finserv", {"match": True, "about": "CARE note"}))
    asyncio.run(explore.web_search(c, sub, "navi rating", field))
    assert c.doc("D3")["url"] == url


def test_choose_example_names_a_real_document(env, monkeypatch):
    c, sub, field = env
    q = qwen(monkeypatch, ("Options:", {"do": "D1"}))
    asyncio.run(explore.choose(c, sub, field))
    assert '{"do":"D1"}' in q.prompts[0]
    sub["done_docs"] |= {"D1"}
    asyncio.run(explore.choose(c, sub, field))
    assert '{"do":"D2"}' in q.prompts[1] and '"D1"' not in q.prompts[1]
