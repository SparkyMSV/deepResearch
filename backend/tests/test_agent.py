import asyncio
import json

import pytest

from app import agent, config, events
from app.ctx import TaskCtx, new_state
from app.db import connect, insert
from app.local_llm import Reply
from tests.fakes import FakeStore, Script, call, tool_out

AR = "http://navi.com/ar.pdf"
PAGES = ["Cover", "Contents", "Asset quality: Gross NPA was 2.1% and Net NPA was 0.9% in FY2025-26."] + [f"p{i}" for i in range(4, 13)]


@pytest.fixture
def env(monkeypatch):
    sent = []
    monkeypatch.setattr(events, "publish", lambda cid, e: sent.append(e))
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="Asset quality", status="running")
    for n in ("Gross NPA %", "Net NPA %"):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=n, entity="Navi Finserv", period="FY2025-26",
               kind="figure", status="open", hint="asset quality table")
    insert(conn, "documents", task_id=tid, key="D1", name="Navi AR", issuer="Navi Finserv", status="verified",
           url=AR, about="Navi AR FY26", covers="financials", pages=len(PAGES))
    c = TaskCtx(conn, 1, tid, "2026-11-24", FakeStore({AR: PAGES}))
    sub = new_state(dict(conn.execute("select * from subtasks").fetchone()))
    rounds = []
    return c, sub, rounds, sent


def review(*items):
    return Reply(json.dumps({"fields": [dict(zip(("field", "action", "hint"), i)) for i in items]}), [], 500)


def found_both(c):
    c.conn.execute("update fields set status='found', value='2.1', quote='q', source_kind='doc'")


def test_review_accept_sets_level_1_and_has_thinking_on(env, monkeypatch):
    c, sub, _, _ = env
    found_both(c)
    s = Script(review((1, "accept", ""), (2, "accept", "")))
    monkeypatch.setattr(agent, "local", s)
    assert asyncio.run(agent.self_review(c, sub)) == "ok"
    assert [r[0] for r in c.conn.execute("select accepted from fields order by id")] == [1, 1]
    assert s.sent[0]["think"] is True and s.sent[0]["tools"] is None


def test_review_reject_clears_and_reopens_retry_keeps_value(env, monkeypatch):
    c, sub, _, _ = env
    found_both(c)
    monkeypatch.setattr(agent, "local", Script(review((1, "reject", "wrong year"), (2, "retry", "check notes"))))
    assert asyncio.run(agent.self_review(c, sub)) == "reopened"
    rows = c.conn.execute("select status, value, hint from fields order by id").fetchall()
    assert [tuple(r) for r in rows] == [("open", None, "wrong year"), ("found", "2.1", "check notes")]


def test_unparseable_review_is_ok(env, monkeypatch):
    c, sub, _, _ = env
    monkeypatch.setattr(agent, "local", Script(Reply("no json here", [], 10)))
    assert asyncio.run(agent.self_review(c, sub)) == "ok"


def test_self_review_server_error_is_ok(env, monkeypatch):
    from app.local_llm import LocalModelError
    c, sub, _, _ = env

    async def boom(*a, **k):
        raise LocalModelError("500")

    monkeypatch.setattr(agent, "local", boom)
    assert asyncio.run(agent.self_review(c, sub)) == "ok"


def test_retry_keeps_the_value_but_sends_the_field_back(env, monkeypatch):
    c, sub, _, _ = env
    c.conn.execute("update fields set status='found', value='2.1', quote='q', source_kind='doc', source_key='D1', page=3 "
                   "where name='Gross NPA %'")
    c.conn.execute("update fields set status='found', value='0.9', quote='q', source_kind='doc' where name='Net NPA %'")
    monkeypatch.setattr(agent, "local", Script(review((1, "retry", "check the consolidated figure"), (2, "accept", ""))))
    assert asyncio.run(agent.self_review(c, sub)) == "reopened"
    r = c.conn.execute("select status, value, hint from fields where name='Gross NPA %'").fetchone()
    assert tuple(r) == ("found", "2.1", "check the consolidated figure")
    assert not agent._closed(c.conn, sub)  # still being rechecked
    assert "reviewer asks to recheck" in agent.opening(c, sub)[0]["content"]


QUOTE = "Gross NPA was 2.1% and Net NPA was 0.9%"


def specialists(monkeypatch, c, fill=()):
    calls = []

    async def explore(ctx, sub, doc, fields, goal, on_round):
        calls.append(("pdf", doc, fields))
        for n in fields:
            if n in fill:
                ctx.conn.execute("update fields set status='found', value='1', quote='q' where id=?",
                                 (agent.all_fields(ctx.conn, sub["id"])[n - 1]["id"],))
            else:
                sub.setdefault("missing_pdf", set()).add(agent.all_fields(ctx.conn, sub["id"])[n - 1]["id"])
        return f"PDF agent on {doc}: done"

    async def web(ctx, sub, fields, goal, on_round):
        calls.append(("web", fields))
        for n in fields:
            sub.setdefault("missing_web", set()).add(agent.all_fields(ctx.conn, sub["id"])[n - 1]["id"])
        return "Web agent: nothing"

    monkeypatch.setattr(agent.pdf_agent, "explore", explore)
    monkeypatch.setattr(agent.web_agent, "search", web)
    return calls


def main(env, script, monkeypatch):
    c, sub, rounds, _ = env
    monkeypatch.setattr(agent.agentkit, "local", script)
    return asyncio.run(agent.run_loop(c, sub, lambda label, pages: rounds.append(label)))


def test_main_delegates_and_appends_reports(env, monkeypatch):
    c, sub, rounds, _ = env
    calls = specialists(monkeypatch, c)
    s = Script(Reply("", [call("explore_document", doc="D1", fields=[1, 2], goal="asset quality")], 500),
               Reply("", [call("done", note="x")], 500))
    assert main(env, s, monkeypatch) == "done"
    assert calls == [("pdf", "D1", [1, 2])] and "PDF agent on D1: done" in tool_out(s.sent[1])
    assert rounds[0] == "→ PDF D1: Gross NPA %, Net NPA %"


def test_delegation_budget(env, monkeypatch):
    monkeypatch.setattr(config, "MAX_DELEGATIONS", 1)
    c, _, _, _ = env
    specialists(monkeypatch, c)
    s = Script(Reply("", [call("search_web", fields=[1], goal="g")], 500),
               Reply("", [call("search_web", 1, fields=[2], goal="g")], 500), Reply("", [call("done")], 500))
    main(env, s, monkeypatch)
    assert "budget" in tool_out(s.sent[2])


def test_not_public_guard_refuses_then_allows(env, monkeypatch):
    c, sub, _, _ = env
    specialists(monkeypatch, c)
    s = Script(Reply("", [call("mark_not_public", field=1, reason="none")], 500),
               Reply("", [call("search_web", fields=[1], goal="g")], 500),
               Reply("", [call("mark_not_public", 1, field=1, reason="none")], 500),
               Reply("", [call("explore_document", doc="D1", fields=[1], goal="g")], 500),
               Reply("", [call("mark_not_public", 2, field=1, reason="not disclosed")], 500), Reply("", [call("done")], 9))
    main(env, s, monkeypatch)
    assert "refused" in tool_out(s.sent[1]) and "PDF agent" in tool_out(s.sent[3])
    assert c.conn.execute("select status from fields where name='Gross NPA %'").fetchone()[0] == "unavailable"


def test_loop_stops_when_all_fields_closed(env, monkeypatch):
    c, _, _, _ = env
    specialists(monkeypatch, c, fill=(1, 2))
    s = Script(Reply("", [call("explore_document", doc="D1", fields=[1, 2], goal="g")], 500))
    assert main(env, s, monkeypatch) == "closed"


def test_opening_lists_documents_with_contents_flag(env):
    c, sub, _, _ = env
    sys = agent.opening(c, sub)[0]["content"]
    assert "D1 Navi AR FY26" in sys and "explore_document" in sys and "Today is 2026-11-24" in sys


def test_gaps_say_where_looked(env, monkeypatch):
    c, sub, _, _ = env
    specialists(monkeypatch, c)
    s = Script(Reply("", [call("search_web", fields=[1, 2], goal="g")], 500), Reply("", [call("done")], 9),
               review(), default=Reply("", [call("done")], 9))
    monkeypatch.setattr(agent.agentkit, "local", s)
    monkeypatch.setattr(agent, "local", s)
    monkeypatch.setattr(config, "MAX_RESTARTS", 0)
    sub["looked"] = {}
    asyncio.run(agent.run_subtask(c, sub["id"]))
    note = c.conn.execute("select note from fields where name='Gross NPA %'").fetchone()[0]
    assert note.startswith("not found (looked in:")


def test_reopening_a_field_clears_who_looked(env, monkeypatch):
    c, sub, _, _ = env
    c.conn.execute("update fields set status='unavailable' where name='Gross NPA %'")
    fid = agent.all_fields(c.conn, sub["id"])[0]["id"]
    sub["missing_web"], sub["missing_pdf"] = {fid}, {fid}
    monkeypatch.setattr(agent, "local", Script(review((1, "retry", "check notes"))))
    asyncio.run(agent.self_review(c, sub))
    assert fid not in sub["missing_web"] and fid not in sub["missing_pdf"]


def test_opening_shows_document_dates_and_prefers_the_fields_period(env):
    c, sub, _, _ = env
    c.set_doc(c.docs()[0], period="April 2025")
    sys = agent.opening(c, sub)[0]["content"]
    assert "dated April 2025" in sys and "field's period" in sys


def test_reviews_judge_substance_not_format():
    from app import final
    for p in (agent.REVIEW_PROMPT, final.FINAL_PROMPT):
        assert "sign" in p and "rounding" in p
