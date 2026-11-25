import asyncio
import json

import httpx
import openai
import pytest

from app import events, final
from app.ctx import TaskCtx
from app.db import connect, insert
from tests.fakes import FakeStore


def f(id, name, value, unit, entity="Navi", period="FY26", status="found"):
    return {"id": id, "name": name, "value": value, "unit": unit, "entity": entity, "period": period, "status": status}


def test_identity_only_when_all_members_present():
    rows = [f(1, "Total Assets", "1,200", "crore"), f(2, "Total Liabilities", "900", "crore")]
    assert final.identity_notes(rows) == {}


def test_identity_mismatch_notes_each_member_and_match_is_silent():
    bad = [f(1, "Total Assets", "1,200", "crore"), f(2, "Total Liabilities", "900", "crore"), f(3, "Net Worth", "250", "crore")]
    notes = final.identity_notes(bad)
    assert set(notes) == {1, 2, 3} and "doesn't add up" in notes[1]
    good = [f(1, "Total Assets", "1,150", "crore"), f(2, "Total Liabilities", "900", "crore"), f(3, "Equity", "250", "crore")]
    assert final.identity_notes(good) == {}


def test_units_normalised_and_percent_skipped():
    rows = [f(1, "Total Assets", "12,000", "million"), f(2, "Total Liabilities", "900", "crore"), f(3, "Equity", "300", "crore")]
    assert final.identity_notes(rows) == {}  # 12,000 million = 1,200 crore = 900 + 300
    npa = [f(1, "Gross NPA", "2.1", "%"), f(2, "Net NPA", "0.9", "%"), f(3, "Provisions", "1.0", "%")]
    assert final.identity_notes(npa) == {}


def test_different_entities_or_periods_not_combined():
    rows = [f(1, "Total Assets", "1,200", "crore"), f(2, "Total Liabilities", "900", "crore", period="FY25"),
            f(3, "Equity", "250", "crore")]
    assert final.identity_notes(rows) == {}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    for name, status, value, acc in (("A", "found", "1", 1), ("B", "found", "2", 0), ("C", "unavailable", None, 0)):
        insert(conn, "fields", task_id=tid, subtask_id=1, name=name, entity="E", period="FY26", status=status,
               value=value, quote="q" if value else None, accepted=acc)
    return TaskCtx(conn, 1, tid, "2026-11-25", FakeStore({}))


def levels(c):
    return [tuple(r) for r in c.conn.execute("select name, status, value, accepted from fields order by id")]


def test_final_review_levels_and_clears(ctx):
    async def ask(msgs):
        ask.msgs = msgs
        return json.dumps({"fields": [{"field": 1, "action": "accept"}, {"field": 2, "action": "reject", "reason": "FY25 figure"},
                                      {"field": 3, "action": "accept"}]})
    asyncio.run(final.final_review(ctx, ask))
    assert levels(ctx) == [("A", "found", "1", 2), ("B", "open", None, 0), ("C", "unavailable", None, 2)]
    assert "[2] B" in ask.msgs[1]["content"] and 'quote: "q"' in ask.msgs[1]["content"]


def test_final_review_failure_keeps_levels(ctx):
    async def junk(msgs):
        return "no json"

    async def down(msgs):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    before = levels(ctx)
    asyncio.run(final.final_review(ctx, junk))
    asyncio.run(final.final_review(ctx, down))
    assert levels(ctx) == before


def test_plural_units_and_bracketed_losses():
    rows = [f(1, "Total Assets", "1,200", "crores"), f(2, "Total Liabilities", "12,000", "millions"),
            f(3, "Equity", "(0)", "crore")]
    assert final.identity_notes(rows) == {}
    loss = [f(1, "Total Income", "100", "crore"), f(2, "Total Expenses", "130", "crore"), f(3, "Tax", "0", "crore"),
            f(4, "Profit After Tax", "(30)", "crore")]
    assert final.identity_notes(loss) == {}


def test_revenue_from_operations_is_not_used_for_the_profit_identity():
    rows = [f(1, "Revenue from operations", "90", "crore"), f(2, "Total Expenses", "130", "crore"),
            f(3, "Tax", "0", "crore"), f(4, "Net Profit", "(30)", "crore")]
    assert final.identity_notes(rows) == {}


def test_final_review_has_no_unavailable_action_and_warns_against_own_beliefs(ctx):
    async def ask(msgs):
        ask.msgs = msgs
        return json.dumps({"fields": [{"field": 2, "action": "unavailable", "reason": "no such entity"}]})
    asyncio.run(final.final_review(ctx, ask))
    assert levels(ctx)[1] == ("B", "found", "2", 0)  # ignored: the reviewer can't declare a field not public
    assert "not from your own beliefs" in ask.msgs[0]["content"] and "unavailable" not in ask.msgs[0]["content"]
