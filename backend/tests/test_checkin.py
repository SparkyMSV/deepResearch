import asyncio
import json

import httpx
import openai
import pytest

from app import checkin, docs, events
from app.ctx import TaskCtx, new_state
from app.db import connect, insert
from tests.fakes import FakeStore


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="s", status="running")
    for name, status, value in (("A", "found", "1"), ("B", "found", "2"), ("C", "open", None), ("D", "unavailable", None)):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=name, entity="E", period="FY26", kind="figure",
               status=status, value=value, quote="q" if value else None, source_kind="web" if value else None)
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({}))
    return c, new_state(dict(conn.execute("select * from subtasks").fetchone()))


def reply(d):
    async def ask(msgs):
        ask.msgs = msgs
        return json.dumps(d)
    return ask


def f(c, name):
    return c.conn.execute("select * from fields where name=?", (name,)).fetchone()


def test_accept_reject_unavailable_retry(env):
    c, sub = env
    ask = reply({"fields": [{"name": "A", "action": "accept"}, {"name": "b", "action": "reject"},
                            {"name": "C", "action": "unavailable", "note": "not disclosed"},
                            {"name": "D", "action": "retry", "hint": "see notes", "search": "E D FY26"}]})
    assert asyncio.run(checkin.review(c, sub, True, ask)) == "reopened"
    assert f(c, "A")["accepted"] == 1
    assert (f(c, "B")["status"], f(c, "B")["value"]) == ("open", None)
    assert (f(c, "C")["status"], f(c, "C")["note"], f(c, "C")["accepted"]) == ("unavailable", "not disclosed", 1)
    assert (f(c, "D")["status"], f(c, "D")["hint"], f(c, "D")["search"]) == ("open", "see notes", "E D FY26")
    assert 'quote: "q"' in ask.msgs[1]["content"]


def test_reopening_dropped_when_not_allowed(env):
    c, sub = env
    ask = reply({"fields": [{"name": "A", "action": "accept"}, {"name": "B", "action": "reject"}]})
    assert asyncio.run(checkin.review(c, sub, False, ask)) == "blocked"
    assert f(c, "A")["accepted"] == 1 and f(c, "B")["value"] == "2"


def test_add_fields_capped_at_three(env):
    c, sub = env
    ask = reply({"add_fields": [{"name": f"N{i}", "entity": "E"} for i in range(5)]})
    assert asyncio.run(checkin.review(c, sub, True, ask)) == "reopened"
    assert c.conn.execute("select count(*) from fields where status='open'").fetchone()[0] == 4


def test_find_document_runs_document_phase(env, monkeypatch):
    c, sub = env
    seen = []

    async def fake_find(ctx, doc):
        seen.append(doc["key"])

    monkeypatch.setattr(checkin, "find_document", fake_find)
    ask = reply({"find_document": {"name": "CARE rationale", "issuer": "E", "type": "rating", "period": "2026"}})
    assert asyncio.run(checkin.review(c, sub, True, ask)) == "reopened" and seen == ["D1"]


def test_unparseable_or_api_error_returns_none(env):
    c, sub = env

    async def junk(msgs):
        return "nope"

    async def down(msgs):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    assert asyncio.run(checkin.review(c, sub, True, junk)) is None
    assert asyncio.run(checkin.review(c, sub, True, down)) is None


def test_prompt_says_document_issuer_is_the_rated_company():
    assert "the rated company, not the agency" in checkin.CHECKIN_PROMPT


def test_checkin_is_told_today(env):
    c, sub = env
    ask = reply({})
    asyncio.run(checkin.review(c, sub, True, ask))
    assert "Today is 2026-11-24" in ask.msgs[1]["content"]


def two_entities(c, sub):
    for ent, val in (("Bank A", "1.1"), ("Bank B", "2.2")):
        insert(c.conn, "fields", task_id=c.task_id, subtask_id=sub["id"], name="Gross NPA %", entity=ent,
               period="FY26", kind="figure", status="found", value=val, quote="q")


def test_checkin_addresses_fields_by_number_so_same_names_stay_apart(env):
    c, sub = env
    two_entities(c, sub)
    ask = reply({"fields": [{"field": 5, "action": "accept"}, {"field": 6, "action": "accept"}]})
    asyncio.run(checkin.review(c, sub, True, ask))
    got = c.conn.execute("select entity, accepted from fields where name='Gross NPA %' order by id").fetchall()
    assert [tuple(r) for r in got] == [("Bank A", 1), ("Bank B", 1)]
    assert "[5] Gross NPA %" in ask.msgs[1]["content"]


def test_hint_only_retry_on_open_field_is_not_a_reopen(env):
    c, sub = env
    assert asyncio.run(checkin.review(c, sub, False, reply({"fields": [{"name": "C", "action": "retry",
                                                                        "hint": "try notes"}]}))) == "steered"
    assert f(c, "C")["hint"] == "try notes"


def test_accepted_values_are_never_undone(env):
    c, sub = env
    c.conn.execute("update fields set accepted=1 where name='A'")
    asyncio.run(checkin.review(c, sub, True, reply({"fields": [{"name": "A", "action": "unavailable"},
                                                               {"name": "A", "action": "reject"}]})))
    assert (f(c, "A")["status"], f(c, "A")["value"], f(c, "A")["accepted"]) == ("found", "1", 1)
