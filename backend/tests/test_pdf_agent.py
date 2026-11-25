import asyncio

import pytest

from app import agentkit, config, events, pdf_agent
from app.ctx import TaskCtx, new_state
from app.db import connect, insert
from app.local_llm import Reply
from tests.fakes import FakeStore, Script, call

U = "http://x/ar.pdf"
TOC = [("Directors report", 4), ("Financial statements", 10), ("Notes to accounts", 15), ("Risk management", 20),
       ("Corporate governance", 25)]


def pages():
    ps = [f"filler {i}" for i in range(1, 31)]
    for t, p in TOC:
        ps[p + 1] = f"{t}\nbody text"
    ps[13] = "Financial statements continued: total borrowings were 6,100 crore at 31 March 2026. Gross NPA 2.1%."
    return ps


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(events, "publish", lambda *a: None)
    conn = connect(":memory:")
    tid = insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    sid = insert(conn, "subtasks", task_id=tid, objective="Debt", status="running")
    for n in ("Total Debt", "Gross NPA %"):
        insert(conn, "fields", task_id=tid, subtask_id=sid, name=n, entity="Nexus", period="FY2025-26", kind="figure",
               status="open")
    insert(conn, "documents", task_id=tid, key="D1", name="AR", status="verified", url=U, about="Nexus AR FY26",
           covers="financials", pages=30)
    c = TaskCtx(conn, 1, tid, "2026-11-25", FakeStore({U: pages()}, {U: TOC}))
    return c, new_state(dict(conn.execute("select * from subtasks").fetchone()))


def run(env, script, monkeypatch, fields=(1, 2)):
    c, sub = env
    monkeypatch.setattr(agentkit, "local", script)
    rounds = []
    out = asyncio.run(pdf_agent.explore(c, sub, "D1", list(fields), "find debt", lambda l, p: rounds.append(l)))
    return out, rounds


Q = "total borrowings were 6,100 crore at 31 March 2026"
END = Reply("", [call("report", found="", missing="", notes="")], 10)


def test_locate_then_read_then_record(env, monkeypatch):
    s = Script(Reply("", [call("locate", query="total borrowings")], 500),
               Reply("", [call("read_pages", **{"from": 12, "to": 12})], 900),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore", quote=Q, page=12)], 1500), END)
    out, rounds = run(env, s, monkeypatch)
    assert "p.12 (Financial statements)" in s.sent[1]["messages"][-1]["content"]
    f = env[0].conn.execute("select value, page, printed_page from fields where name='Total Debt'").fetchone()
    assert tuple(f) == ("6,100", 14, 12)
    assert rounds[:3] == ["D1 locate total borrowings", "D1 pp. 12", "recorded Total Debt = 6,100 crore"]
    assert "recorded: [1] Total Debt = 6,100 crore (p.12)" in out


def test_record_refused_from_snippet_only(env, monkeypatch):
    s = Script(Reply("", [call("locate", query="total borrowings")], 500),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore", quote=Q, page=12)], 900), END)
    run(env, s, monkeypatch)
    assert "read the page first" in s.sent[2]["messages"][-1]["content"]


def test_read_pages_max_two_and_reread_refused(env, monkeypatch):
    s = Script(Reply("", [call("read_pages", **{"from": 5, "to": 9})], 500),
               Reply("", [call("read_pages", 1, **{"from": 5, "to": 5})], 500), END)
    run(env, s, monkeypatch)
    first = s.sent[1]["messages"][-1]["content"]
    assert "[page 5]" in first and "[page 6]" in first and "[page 7]" not in first
    assert "already read page 5" in s.sent[2]["messages"][-1]["content"]


def test_compaction_keeps_recording_possible(env, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TOKENS", 400)
    c, _ = env
    c.store.docs[U][13] = ("x " * 1500) + Q
    s = Script(Reply("", [call("read_pages", **{"from": 12, "to": 12})], 900),
               Reply("", [call("read_pages", 1, **{"from": 13, "to": 13})], 900),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore", quote=Q, page=12)], 900), END)
    run(env, s, monkeypatch)
    assert "text removed to save space" in str(s.sent[2]["messages"])
    assert c.conn.execute("select value from fields where name='Total Debt'").fetchone()[0] == "6,100"


def test_report_ends_and_marks_missing(env, monkeypatch):
    c, sub = env
    s = Script(Reply("", [call("report", found="", missing="2", notes="NPA not in this report")], 10))
    out, _ = run(env, s, monkeypatch)
    assert "missing: [1] Total Debt, [2] Gross NPA %" in out and "NPA not in this report" in out
    assert sub["missing_pdf"] >= {1, 2} or len(sub["missing_pdf"]) == 2


def test_step_budget_ends_with_auto_report(env, monkeypatch):
    monkeypatch.setattr(config, "PDF_STEPS", 2)
    s = Script(*[Reply("", [call("locate", i, query=f"q{i}")], 100) for i in range(5)])
    out, _ = run(env, s, monkeypatch)
    assert out.startswith("PDF agent on D1") and "step budget" in out


def test_same_printed_page_in_two_sections(env, monkeypatch):
    c, sub = env
    toc = [("Board report", 3), ("Standalone statements", 1), ("Standalone notes", 6), ("Consolidated statements", 1),
           ("Consolidated notes", 6)]
    ps = [f"filler {i}" for i in range(1, 31)]
    for (t, _), pdf in zip(toc, (5, 10, 15, 20, 25)):
        ps[pdf - 1] = f"{t}\nbody"
    ps[20] = "Consolidated: total borrowings were 6,100 crore at 31 March 2026"
    c.store.docs[U], c.store.toc[U] = ps, toc
    s = Script(Reply("", [call("read_pages", **{"from": 2, "to": 2, "section": "Standalone statements"})], 500),
               Reply("", [call("read_pages", 1, **{"from": 2, "to": 2, "section": "Consolidated statements"})], 500),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore", quote=Q, page=2,
                               section="Consolidated statements")], 500), END)
    run(env, s, monkeypatch)
    assert "already read" not in s.sent[2]["messages"][-1]["content"]
    assert tuple(c.conn.execute("select page, printed_page from fields where name='Total Debt'").fetchone()) == (21, 2)


def test_spread_pages_read_once_with_both_halves(env, monkeypatch):
    c, sub = env
    ps = ["letter", "blank"] + [f"sheet {i}" for i in range(3, 41)]
    for t, pr in TOC:
        ps[pr // 2 + 2 - 1] = f"{t}\nbody text"
    ps[8] = "Financial statements (contd.) " + "x" * 5000 + " total borrowings were 6,100 crore at 31 March 2026"
    c.store = FakeStore({U: ps}, {U: TOC})
    c.store.spreads = {U}
    s = Script(Reply("", [call("read_pages", **{"from": 14, "to": 15})], 900),
               Reply("", [call("record_value", field=1, value="6,100", unit="crore", quote=Q, page=15)], 1500), END)
    out, _ = run(env, s, monkeypatch)
    read = s.sent[1]["messages"][-1]["content"]
    assert "[pages 14-15]" in read and "6,100 crore" in read and "15" not in read.split("[pages 14-15]")[0]
    f = c.conn.execute("select value, page, printed_page from fields where name='Total Debt'").fetchone()
    assert tuple(f) == ("6,100", 9, 15)
