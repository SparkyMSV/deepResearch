import asyncio
import json

import pytest

from app import docs, events, fields
from app.ctx import TaskCtx, new_state
from app.db import Hit, connect, insert
from tests.fakes import FakeStore, ScriptedQwen

NAVI = "http://navi.com/ar-2025-26.pdf"
NAZARA = "http://cdn.x/Annual+Report+FY+25-26.pdf"


def make(monkeypatch, store, qwen, hits=()):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    monkeypatch.setattr(docs, "qwen", qwen)
    monkeypatch.setattr(fields, "qwen", qwen)
    searched = []
    monkeypatch.setattr(docs, "ddg", lambda q: searched.append(q) or list(hits))
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="s", status="running")
    insert(conn, "fields", task_id=tid, subtask_id=sid, name="Gross NPA %", entity="Navi Finserv", period="FY2025-26",
           kind="figure", status="open")
    c = TaskCtx(conn, 1, tid, "2026-11-24", store)
    c.searched = searched
    return c, new_state(dict(conn.execute("select * from subtasks").fetchone()))


def plan_doc(c):
    insert(c.conn, "documents", task_id=c.task_id, key="D1", name="Navi Finserv annual report FY2025-26",
           issuer="Navi Finserv Limited", type="annual report", period="FY2025-26", status="planned",
           search="Navi Finserv annual report 2025-26 pdf", domains='["navi.com"]')
    return c.doc("D1")


def test_find_document_screens_then_verifies(monkeypatch):
    store = FakeStore({NAVI: ["Navi Finserv Limited Annual Report 2025-26"], NAZARA: ["Nazara"]})
    q = ScriptedQwen(("Another year is fine", {"match": True, "about": "Navi Finserv AR FY26", "covers": "financials"}))
    hits = [Hit("Annual Report FY 25-26", NAZARA, "Nazara Technologies"), Hit("Navi Finserv annual report", NAVI, "")]
    c, _ = make(monkeypatch, store, q, hits)
    asyncio.run(docs.find_document(c, plan_doc(c)))
    d = c.doc("D1")
    assert (d["status"], d["url"], d["about"], d["pages"]) == ("verified", NAVI, "Navi Finserv AR FY26", 1)
    assert all(u != NAZARA for u, _ in store.reads)


def test_find_document_not_found_after_three_rejections(monkeypatch):
    urls = [f"http://navi.com/{i}.pdf" for i in range(4)]
    store = FakeStore({u: ["Navi Finserv something"] for u in urls})
    q = ScriptedQwen(("Another year is fine", {"match": False}))
    c, _ = make(monkeypatch, store, q, [Hit("Navi Finserv", u, "") for u in urls])
    asyncio.run(docs.find_document(c, plan_doc(c)))
    assert c.doc("D1")["status"] == "not_found" and len(q.prompts) == 3


def test_verify_dedupes_concurrent_calls(monkeypatch):
    store = FakeStore({NAVI: ["Navi Finserv annual report " * 20]})
    q = ScriptedQwen(("Is this", {"match": True, "about": "a", "covers": "b"}))
    c, _ = make(monkeypatch, store, q)

    async def both():
        exp = {"issuer": "Navi", "type": "annual report", "period": "FY26"}
        return await asyncio.gather(docs.verify(c, NAVI, exp), docs.verify(c, NAVI, exp))

    a, b = asyncio.run(both())
    assert a == b and a["pages"] == 1 and len(q.prompts) == 1 and c.pages_used == 0


def test_verify_download_error_is_none(monkeypatch):
    c, _ = make(monkeypatch, FakeStore({}, broken={NAVI}), ScriptedQwen())
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi", "type": "any", "period": "FY26"})) is None


def verified(c, url, pages):
    insert(c.conn, "documents", task_id=c.task_id, key="D1", name="Navi AR", issuer="Navi", status="verified",
           url=url, pages=pages)


def test_visit_contents_then_pages(monkeypatch):
    pages = [f"page {i}" for i in range(1, 21)]
    pages[5] = "Directors report"
    toc = [("Intro", 2), ("Directors report", 4), ("Accounts", 9), ("Notes", 12), ("Risk", 15)]
    store = FakeStore({NAVI: pages}, {NAVI: toc})
    q = ScriptedQwen(("Contents:", {"open": 2}), ("Page 6 of", {"found": [], "next": 7}),
                     ("Page 7 of", {"found": [], "next": "leave"}))
    c, sub = make(monkeypatch, store, q)
    verified(c, NAVI, 20)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 2
    assert sub["read"]["D1"] == {6, 7} and c.pages_used == 2


def test_visit_short_doc_reads_from_page_one(monkeypatch):
    store = FakeStore({NAVI: ["p1", "p2", "p3"]})
    q = ScriptedQwen(("Looking for", {"found": [], "next": 2}))
    c, sub = make(monkeypatch, store, q)
    verified(c, NAVI, 3)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 3
    assert [p for _, p in store.reads] == [1, 2, 3] and "D1" in sub["done_docs"]


def test_visit_stops_at_six_pages(monkeypatch):
    store = FakeStore({NAVI: [f"p{i}" for i in range(1, 9)]})
    q = ScriptedQwen(("Looking for", lambda p: {"found": [], "next": int(p.split("Page ")[1].split()[0]) + 1}))
    c, sub = make(monkeypatch, store, q)
    verified(c, NAVI, 8)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 6


def test_visit_broken_download_marks_doc_done(monkeypatch):
    c, sub = make(monkeypatch, FakeStore({}, broken={NAVI}), ScriptedQwen())
    verified(c, NAVI, 3)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 0 and "D1" in sub["done_docs"]


def test_add_found_pdf_becomes_next_document(monkeypatch):
    store = FakeStore({NAVI: ["CARE rating rationale for Navi Finserv"]})
    q = ScriptedQwen(("about Navi Finserv", {"match": True, "about": "CARE rationale", "covers": "rating"}))
    c, _ = make(monkeypatch, store, q)
    plan_doc(c)
    field = dict(c.conn.execute("select * from fields").fetchone())
    d = asyncio.run(docs.add_found_pdf(c, NAVI, field))
    assert (d["key"], d["status"], d["name"]) == ("D2", "verified", "CARE rationale")


def test_page_ranges():
    assert docs.page_ranges({1, 2, 3, 7, 9, 10}) == "pp. 1–3, 7, 9–10"


def test_verify_asks_about_the_company_not_the_exact_period(monkeypatch):
    store = FakeStore({NAVI: ["CARE Ratings: Navi Finserv Limited, December 2025 " * 20]})
    q = ScriptedQwen(("Another year is fine", {"match": True, "about": "CARE rationale"}))
    c, _ = make(monkeypatch, store, q)
    r = asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi Finserv Limited", "type": "rating rationale",
                                          "period": "FY2025-26"}))
    assert r["about"] == "CARE rationale"


def test_leaving_without_reading_sets_the_document_aside(monkeypatch):
    store = FakeStore({NAVI: [f"p{i}" for i in range(1, 21)]}, {NAVI: [("A", 2), ("B", 4), ("C", 6), ("D", 8), ("E", 9)]})
    c, sub = make(monkeypatch, store, ScriptedQwen(("Contents:", {"leave": True})))
    verified(c, NAVI, 20)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 0 and "D1" in sub["done_docs"]


def test_no_contents_page_offers_pages_and_stray_open_reads_first_unread(monkeypatch):
    store = FakeStore({NAVI: [f"p{i}" for i in range(1, 21)]})  # no contents entries
    q = ScriptedQwen(("Opening lines:", {"open": 1}), ("Looking for", {"found": [], "next": "leave"}))
    c, sub = make(monkeypatch, store, q)
    verified(c, NAVI, 20)
    assert asyncio.run(docs.visit(c, sub, "D1")) == 1 and sub["read"]["D1"] == {1}
    assert '{"page":2}' in q.prompts[0] and '"open"' not in q.prompts[0]


def test_verdict_for_one_company_does_not_block_another(monkeypatch):
    store = FakeStore({NAVI: ["CARE Ratings: Navi Finserv Limited " * 20]})
    q = ScriptedQwen(("about CRISIL", {"match": False}), ("about Navi Finserv", {"match": True, "about": "CARE"}))
    c, _ = make(monkeypatch, store, q)
    t = {"type": "rating rationale", "period": "FY26"}
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "CRISIL", **t})) is None
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi Finserv Limited", **t}))["about"] == "CARE"
