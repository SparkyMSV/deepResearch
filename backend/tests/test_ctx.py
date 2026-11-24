from app import ctx as C
from app import events
from app.db import connect, insert
from tests.fakes import FakeStore


def test_set_field_publishes_event(monkeypatch):
    sent = []
    monkeypatch.setattr(events, "publish", lambda cid, e: sent.append(e))
    conn = connect(":memory:")
    fid = insert(conn, "fields", task_id=1, subtask_id=2, name="GNPA", status="open", quote="q")
    c = C.TaskCtx(conn, conv_id=7, task_id=1, today="2026-11-24", store=FakeStore({}))
    row = dict(conn.execute("select * from fields where id=?", (fid,)).fetchone())
    c.set_field(row, status="found", value="2.1")
    assert conn.execute("select status, value from fields").fetchone()[:] == ("found", "2.1")
    assert sent[0]["type"] == "field" and sent[0]["field"]["status"] == "found" and "quote" not in sent[0]["field"]


def test_pages_budget_and_docs(monkeypatch):
    monkeypatch.setattr(C.config, "MAX_PAGES", 2)
    conn = connect(":memory:")
    insert(conn, "documents", task_id=1, key="D1", name="AR", status="verified")
    insert(conn, "documents", task_id=1, key="D2", name="X", status="planned")
    c = C.TaskCtx(conn, 7, 1, "2026-11-24", FakeStore({}))
    assert [d["key"] for d in c.docs()] == ["D1"] and c.doc("D2")["name"] == "X"
    c.pages_used = 2
    assert not c.pages_left()


def test_date_note_says_last_financial_year_has_ended():
    n = C.date_note("2026-11-24")
    assert "FY2025-26 (April 2025 to March 2026) has ended" in n and "Calendar year 2025 has ended" in n
    assert "FY2024-25" in C.date_note("2026-02-10")
