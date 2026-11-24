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


def row(c, name):
    return c.conn.execute("select * from fields where name=?", (name,)).fetchone()


TABLE = "| Metric | FY26 |\n|---|---|\n| **Gross NPA** | **2.1%** |\n| Net NPA – ratio | 0.9% |"
SRC = {"url": "http://x/ar.pdf", "title": "AR", "key": "D1", "page": 112, "kind": "doc", "text": PAGE}


def test_status_line(env):
    c, sub = env
    c.conn.execute("update fields set status='found' where name='Gross NPA %'")
    assert fields.status_line(c.conn, sub["id"]) == "filled 1/2: [1] ✓ [2] –"
    c.conn.execute("update fields set status='unavailable' where name='Net NPA %'")
    assert fields.status_line(c.conn, sub["id"]) == "filled 1/2: [1] ✓ [2] not public?"


def test_record_ok_and_evidence(env):
    c, sub = env
    out = fields.record(c, sub, 1, "2.1", "%", "FY2025-26", "GNPA was 2.1% and NNPA", SRC)
    assert out == "recorded [1] Gross NPA % = 2.1 % (D1 p.112)"
    r = row(c, "Gross NPA %")
    assert (r["status"], r["value"], r["source_key"], r["page"], r["accepted"]) == ("found", "2.1", "D1", 112, 0)
    assert c.conn.execute("select count(*) from evidence where page=112").fetchone()[0] == 1


def test_record_rejects_quote_not_on_page_and_bad_field(env):
    c, sub = env
    assert fields.record(c, sub, 1, "2.1", "%", "", "GNPA stood at 2.1 percent", SRC).startswith("rejected: quote not")
    assert fields.record(c, sub, 1, "2.1", "%", "", "2.1%", SRC).startswith("rejected: quote too short")
    assert fields.record(c, sub, 1, "5.5", "%", "", "GNPA was 2.1% and NNPA", SRC).startswith("rejected: the quote doesn't contain")
    assert fields.record(c, sub, 9, "2.1", "%", "", "GNPA was 2.1% and NNPA", SRC).startswith("rejected: no field 9")


def test_record_never_overwrites_accepted(env):
    c, sub = env
    fields.record(c, sub, 1, "2.1", "%", "", "GNPA was 2.1% and NNPA", SRC)
    c.conn.execute("update fields set accepted=1")
    assert fields.record(c, sub, 1, "0.9", "%", "", "NNPA was 0.9% as of 31 March", SRC).startswith("rejected: already accepted")


def test_record_web_never_replaces_document(env):
    c, sub = env
    fields.record(c, sub, 1, "2.1", "%", "", "GNPA was 2.1% and NNPA", SRC)
    web = dict(SRC, kind="web", key=None, page=None, url="http://news")
    assert fields.record(c, sub, 1, "0.9", "%", "", "NNPA was 0.9% as of 31 March", web).startswith("rejected: a document value")
    assert row(c, "Gross NPA %")["value"] == "2.1"


def test_record_markdown_table_quote(env):
    c, sub = env
    src = dict(SRC, text=TABLE)
    assert fields.record(c, sub, 1, "2.1", "%", "", "Gross NPA 2.1%", src).startswith("recorded")
