import asyncio
from dataclasses import replace

import pytest

from app import config, docs, events, tools
from app.ctx import TaskCtx, new_state
from app.db import Hit, connect, insert
from tests.fakes import FakeStore, ScriptedQwen

AR = "http://navi.com/ar.pdf"
PAGES = ["Cover", "Contents", "Asset quality: Gross NPA was 2.1% and Net NPA was 0.9% in FY2025-26."] + [f"p{i}" for i in range(4, 13)]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="s", status="running")
    for n in ("Gross NPA %", "Net NPA %"):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=n, entity="Navi Finserv", period="FY2025-26",
               kind="figure", status="open")
    insert(conn, "documents", task_id=tid, key="D1", name="Navi AR", issuer="Navi Finserv", status="verified",
           url=AR, about="Navi AR FY26", covers="financials", pages=len(PAGES))
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({AR: PAGES}))
    return c, new_state(dict(conn.execute("select * from subtasks").fetchone()))


def run(c, sub, name, **args):
    return asyncio.run(tools.run_tool(c, sub, name, args))


HITS = [Hit("Navi Finserv results", "http://a.com/x", "Navi Finserv GNPA"), Hit("Other", "http://b.com/y", "")]


def test_search_lists_and_refuses_repeat(env, monkeypatch):
    c, sub = env
    monkeypatch.setattr(tools, "ddg", lambda q, n=None: list(HITS))
    text, pages = run(c, sub, "search", query="Navi Finserv gross NPA FY26")
    assert "[1] Navi Finserv results — http://a.com/x" in text and pages == 0
    assert run(c, sub, "search", query="navi finserv gross npa fy26")[0].startswith("already tried")


def test_open_result_html_sets_last_page(env, monkeypatch):
    c, sub = env
    monkeypatch.setattr(tools, "ddg", lambda q, n=None: list(HITS))
    monkeypatch.setattr(tools, "fetch", lambda h: replace(h, text="Gross NPA was 2.1% says the press"))
    run(c, sub, "search", query="navi gnpa")
    text, pages = run(c, sub, "open_result", n=1)
    assert pages == 1 and "Gross NPA was 2.1%" in text and sub["last_page"]["kind"] == "web"
    assert run(c, sub, "open_result", n=7)[0].startswith("error")


def test_open_result_pdf_becomes_document(env, monkeypatch):
    c, sub = env
    pdf = "http://care.com/navi.pdf"
    c.store.docs[pdf] = ["CARE Ratings: Navi Finserv Limited rationale " * 10]
    monkeypatch.setattr(tools, "ddg", lambda q, n=None: [Hit("Navi Finserv CARE", pdf, "")])
    monkeypatch.setattr(docs, "qwen", ScriptedQwen(("about Navi Finserv", {"match": True, "about": "CARE note"})))
    run(c, sub, "search", query="navi rating")
    assert "Added as D2" in run(c, sub, "open_result", n=1)[0]


def test_pdf_page_refuses_reread_and_bad_range(env):
    c, sub = env
    text, pages = run(c, sub, "pdf_page", doc="D1", page=3)
    assert "Gross NPA was 2.1%" in text and pages == 1 and sub["last_page"]["page"] == 3
    assert run(c, sub, "pdf_page", doc="D1", page=3)[0].startswith("already read")
    assert run(c, sub, "pdf_page", doc="D1", page=99)[0].startswith("error")
    assert run(c, sub, "pdf_page", doc="D9", page=1)[0].startswith("error")


def test_pdf_find(env):
    c, sub = env
    assert "gross npa: 3" in run(c, sub, "pdf_find", doc="D1", terms=["gross npa"])[0]


def test_record_uses_last_page(env):
    c, sub = env
    assert run(c, sub, "record_value", field=1, value="2.1", quote="Gross NPA was 2.1%")[0].startswith("rejected: read a page")
    run(c, sub, "pdf_page", doc="D1", page=3)
    text, _ = run(c, sub, "record_value", field=1, value="2.1", unit="%", period="FY2025-26", quote="Gross NPA was 2.1% and Net")
    assert text.startswith("recorded [1] Gross NPA % = 2.1 % (D1 p.3)")


def test_bad_args_and_unknown_tool_return_error_text(env):
    c, sub = env
    assert run(c, sub, "pdf_page", doc="D1")[0].startswith("error")
    assert run(c, sub, "fly", x=1)[0].startswith("error: unknown tool")
    assert run(c, sub, "pdf_page", _raw="{bad")[0].startswith("error")


def test_page_budget_message(env, monkeypatch):
    c, sub = env
    monkeypatch.setattr(config, "MAX_PAGES", 1)
    run(c, sub, "pdf_page", doc="D1", page=3)
    assert run(c, sub, "pdf_page", doc="D1", page=4)[0].startswith("Page budget used up")


def test_mark_not_public_and_done(env):
    c, sub = env
    assert run(c, sub, "mark_not_public", field=2, reason="not disclosed")[0].startswith("marked [2] Net NPA % not public")
    assert c.conn.execute("select status, note from fields where name='Net NPA %'").fetchone()[:] == ("unavailable", "Bonsai: not disclosed")
    assert run(c, sub, "done", note="all found")[0].startswith("ok") and sub["done_note"] == "all found"


def test_results_end_with_status_line(env):
    c, sub = env
    assert run(c, sub, "pdf_page", doc="D1", page=1)[0].endswith("filled 0/2: [1] – [2] –")


def test_tool_schemas_named():
    assert [t["function"]["name"] for t in tools.TOOLS] == [
        "search", "open_result", "pdf_contents", "pdf_find", "pdf_page", "record_value", "mark_not_public", "done"]
