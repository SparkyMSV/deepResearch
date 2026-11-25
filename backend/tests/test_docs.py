import asyncio
import json

import pytest

from app import docs, events
from app.ctx import TaskCtx, new_state
from app.db import Hit, connect, insert
from tests.fakes import FakeStore, ScriptedQwen

NAVI = "http://navi.com/ar-2025-26.pdf"
NAZARA = "http://cdn.x/Annual+Report+FY+25-26.pdf"


def make(monkeypatch, store, qwen, hits=()):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    monkeypatch.setattr(docs, "qwen", qwen)
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


def test_verdict_for_one_company_does_not_block_another(monkeypatch):
    store = FakeStore({NAVI: ["CARE Ratings: Navi Finserv Limited " * 20]})
    q = ScriptedQwen(("about CRISIL", {"match": False}), ("about Navi Finserv", {"match": True, "about": "CARE"}))
    c, _ = make(monkeypatch, store, q)
    t = {"type": "rating rationale", "period": "FY26"}
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "CRISIL", **t})) is None
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi Finserv Limited", **t}))["about"] == "CARE"


def test_find_document_keeps_searching_hosts_until_enough_candidates(monkeypatch):
    store = FakeStore({NAVI: ["Navi Finserv annual report"]})
    q = ScriptedQwen(("Another year is fine", {"match": True, "about": "Navi AR"}))
    c, _ = make(monkeypatch, store, q)
    by_query = {"site:navi.com": [Hit("Navi Finserv annual report", NAVI, "")]}
    monkeypatch.setattr(docs, "ddg", lambda qy: c.searched.append(qy) or next((v for k, v in by_query.items() if k in qy), []))
    asyncio.run(docs.find_document(c, plan_doc(c)))
    assert c.doc("D1")["status"] == "verified" and len(c.searched) == 3 and "site:navi.com" in c.searched[2]

def test_verify_reports_a_paused_model(monkeypatch):
    store = FakeStore({NAVI: ["Navi Finserv annual report " * 20]})

    async def paused(messages, priority=None, max_tokens=800, on_token=None, on_wait=None):
        on_wait("memory")
        return '{"match": true, "about": "a", "covers": "b"}'

    c, _ = make(monkeypatch, store, paused)
    sent = []
    monkeypatch.setattr(events, "publish", lambda cid, e: sent.append(e))
    asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi", "type": "any", "period": "FY26"}))
    assert any(e["type"] == "message" and "memory" in e["message"]["content"].lower() for e in sent)


def test_verify_reads_past_a_cover_letter(monkeypatch):
    letter = "To BSE Limited. Sub: Submission of Annual Report for FY2025-26. Yours faithfully, Company Secretary."
    store = FakeStore({NAVI: [letter, "Navi Finserv Limited Annual Report 2025-26. Financial statements. " * 40]})
    q = ScriptedQwen(("Another year is fine", {"match": True, "about": "Navi Finserv annual report FY2025-26"}))
    c, _ = make(monkeypatch, store, q)
    asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi Finserv", "type": "annual report", "period": "FY26"}))
    assert "Financial statements" in q.prompts[0] and "cover letter" in q.prompts[0]


def test_found_pdf_is_prepared_in_the_background(monkeypatch):
    store = FakeStore({NAVI: ["CARE rationale for Navi Finserv " * 20]})
    q = ScriptedQwen(("about Navi Finserv", {"match": True, "about": "CARE note"}))
    c, _ = make(monkeypatch, store, q)
    started = []

    async def slow_prepare(ctx, doc):
        started.append(doc["key"])
        await asyncio.sleep(3600)

    monkeypatch.setattr(docs, "_prepare", slow_prepare)
    field = dict(c.conn.execute("select * from fields").fetchone())

    async def go():
        d = await asyncio.wait_for(docs.add_found_pdf(c, NAVI, field), 2)  # returns without waiting for preparation
        await asyncio.sleep(0)
        return d

    assert asyncio.run(go())["key"] == "D1" and started == ["D1"]


def test_verify_records_the_period_the_document_covers(monkeypatch):
    store = FakeStore({NAVI: ["ICRA rating rationale, Navi Finserv Limited, April 2025 " * 20]})
    q = ScriptedQwen(("dated", {"match": True, "about": "ICRA rationale", "dated": "April 2025"}))
    c, _ = make(monkeypatch, store, q)
    r = asyncio.run(docs.verify(c, NAVI, {"issuer": "Navi Finserv Limited", "type": "rating rationale",
                                          "period": "FY2025-26"}))
    assert r["period"] == "April 2025"


def test_verify_asks_about_the_entity_itself_not_a_related_company(monkeypatch):
    store = FakeStore({NAVI: ["CARE Ratings: K Raheja IT Park (Hyderabad) Limited bank facilities " * 20]})
    q = ScriptedQwen(("not a subsidiary", {"match": False}))
    c, _ = make(monkeypatch, store, q)
    assert asyncio.run(docs.verify(c, NAVI, {"issuer": "Mindspace REIT", "type": "rating rationale",
                                             "period": "FY2025-26"})) is None
    assert "not a subsidiary" in q.prompts[0]
