import asyncio
import json

from app import plan
from app.db import connect, insert

EXAMPLE = {
    "documents": [{"key": "D1", "name": "Navi Finserv Limited annual report FY2025-26", "issuer": "Navi Finserv Limited",
                   "type": "annual report", "period": "FY2025-26", "search": "Navi Finserv annual report 2025-26 pdf",
                   "domains": ["navi.com"]}],
    "subtasks": [{"objective": "Asset quality of Navi Finserv", "fields": [
        {"name": "Gross NPA %", "entity": "Navi Finserv Limited", "period": "FY2025-26", "kind": "figure",
         "sources": ["D1"], "search": "Navi Finserv gross NPA March 2026", "hint": "asset quality table"}]}],
}


def setup(monkeypatch, *replies):
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="obj", status="running")
    prompts, queue = [], list(replies)

    async def fake_cloud(role, messages, conn_):
        assert role == "plan"
        prompts.append(messages)
        return queue.pop(0)

    monkeypatch.setattr(plan, "cloud", fake_cloud)
    return conn, tid, prompts


def run(conn, tid):
    return asyncio.run(plan.make_plan(conn, tid, "obj", "2026-11-24"))


def test_plan_inserts_documents_subtasks_fields(monkeypatch):
    conn, tid, _ = setup(monkeypatch, json.dumps(EXAMPLE))
    sids = run(conn, tid)
    assert len(sids) == 1
    d = conn.execute("select * from documents").fetchone()
    assert (d["key"], d["status"], json.loads(d["domains"])) == ("D1", "planned", ["navi.com"])
    f = conn.execute("select * from fields").fetchone()
    assert (f["subtask_id"], f["sources"], f["hint"], f["status"]) == (sids[0], '["D1"]', "asset quality table", "open")
    assert conn.execute("select status from subtasks").fetchone()[0] == "queued"


def test_today_in_prompt(monkeypatch):
    conn, tid, prompts = setup(monkeypatch, json.dumps(EXAMPLE))
    run(conn, tid)
    assert "2026-11-24" in prompts[0][0]["content"]


def test_caps_and_cleanup(monkeypatch):
    docs = [dict(EXAMPLE["documents"][0], key=f"X{i}") for i in range(8)]
    subs = [{"objective": f"s{i}", "fields": [{"name": f"f{i}-{j}", "entity": "E", "kind": "weird",
                                               "sources": ["X0", "D9"]} for j in range(10)]} for i in range(10)]
    conn, tid, _ = setup(monkeypatch, json.dumps({"documents": docs, "subtasks": subs}))
    assert len(run(conn, tid)) == 8
    assert conn.execute("select count(*) from documents").fetchone()[0] == 6
    assert [r[0] for r in conn.execute("select key from documents order by id")] == [f"D{i}" for i in range(1, 7)]
    rows = conn.execute("select * from fields").fetchall()
    assert len(rows) == 40 and max(conn.execute(
        "select count(*) from fields group by subtask_id").fetchall(), key=lambda r: r[0])[0] <= 8
    assert {(r["sources"], r["period"], r["kind"]) for r in rows} == {('["D1"]', "latest", "fact")}


def test_bad_json_retries_then_falls_back(monkeypatch):
    conn, tid, prompts = setup(monkeypatch, "nope", "still nope")
    assert len(run(conn, tid)) == 1 and len(prompts) == 2
    f = conn.execute("select * from fields").fetchone()
    assert (f["name"], f["kind"], f["period"]) == ("obj", "fact", "latest")
