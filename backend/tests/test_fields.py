import asyncio

import pytest

from app import events, fields
from app.ctx import TaskCtx, new_state
from app.db import connect, insert
from tests.fakes import FakeStore, ScriptedQwen

PAGE = "Asset quality\nThe Company’s GNPA\nwas 2.1% and NNPA was 0.9% as of 31 March 2026."
DOC = {"url": "http://x/ar.pdf", "title": "AR", "key": "D1", "page": 112, "kind": "doc"}
WEB = {"url": "http://news/x", "title": "News", "key": None, "page": None, "kind": "web"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="s", status="running")
    for name in ("Gross NPA %", "Net NPA %"):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=name, entity="Navi Finserv", period="FY2025-26",
               kind="figure", status="open")
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({}))
    sub = new_state(dict(conn.execute("select * from subtasks").fetchone()))
    return c, sub


def use(monkeypatch, *replies):
    q = ScriptedQwen(("Looking for", list(replies)))
    monkeypatch.setattr(fields, "qwen", q)
    return q


def found(*items, nxt=None):
    d = {"found": [dict(zip(("field", "value", "quote"), i), unit="%", period="FY2025-26") for i in items]}
    return d if nxt is None else {**d, "next": nxt}


def row(c, name):
    return c.conn.execute("select * from fields where name=?", (name,)).fetchone()


def test_fills_by_number_or_name(env, monkeypatch):
    c, sub = env
    use(monkeypatch, found((1, "2.1", "GNPA was 2.1%"), ("net npa %", "0.9", "NNPA was 0.9%"), nxt="leave"))
    ids, nxt = asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=True))
    assert len(ids) == 2 and nxt == "leave"
    r = row(c, "Gross NPA %")
    assert (r["status"], r["value"], r["source_key"], r["page"], r["source_kind"]) == ("found", "2.1", "D1", 112, "doc")
    assert c.conn.execute("select count(*) from evidence where page=112").fetchone()[0] == 2


def test_quote_must_be_verbatim(env, monkeypatch):
    c, sub = env
    use(monkeypatch, found((1, "2.1", "GNPA stood at 2.1 percent")))
    assert asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=False)) == ([], None)
    assert c.conn.execute("select count(*) from evidence").fetchone()[0] == 0


def test_curly_quotes_and_newlines_normalised(env, monkeypatch):
    c, sub = env
    use(monkeypatch, found((1, "2.1", "The Company's GNPA was 2.1%")))
    ids, _ = asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=False))
    assert ids == [row(c, "Gross NPA %")["id"]]


def test_document_beats_web_never_reverse(env, monkeypatch):
    c, sub = env
    use(monkeypatch, found((1, "2.0", "GNPA was 2.1%")), found((1, "2.1", "GNPA was 2.1%")),
        found((1, "9.9", "GNPA was 2.1%")))
    asyncio.run(fields.read_page(c, sub, PAGE, WEB, nav=False))
    asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=False))
    assert row(c, "Gross NPA %")["value"] == "2.1"
    asyncio.run(fields.read_page(c, sub, PAGE, WEB, nav=False))
    assert row(c, "Gross NPA %")["value"] == "2.1"
    c.conn.execute("update fields set accepted=1")
    use(monkeypatch, found((1, "7.7", "GNPA was 2.1%")))
    asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=False))
    assert row(c, "Gross NPA %")["value"] == "2.1"


def test_unparseable_twice_leaves(env, monkeypatch):
    c, sub = env
    q = use(monkeypatch, "not json")
    assert asyncio.run(fields.read_page(c, sub, PAGE, DOC, nav=True)) == ([], "leave")
    assert len(q.prompts) == 2


def test_page_counts_against_budget_and_empty_page_shown(env, monkeypatch):
    c, sub = env
    q = use(monkeypatch, found())
    asyncio.run(fields.read_page(c, sub, "", DOC, nav=True))
    assert c.pages_used == 1 and "(no text on this page)" in q.prompts[0]
