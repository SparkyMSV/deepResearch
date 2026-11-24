import asyncio
import json

import httpx
import openai

from app import config, docs, events, explore, fields, plan, task
from app.cloud_llm import QuotaExhausted
from app.ctx import TaskCtx
from app.db import connect, insert
from tests.fakes import FakeStore, ScriptedQwen

AR = "http://navi.com/ar.pdf"
PLAN = {"documents": [], "subtasks": [{"objective": "Asset quality", "fields": [
    {"name": "Gross NPA %", "entity": "Navi Finserv", "period": "FY2025-26", "kind": "figure"}]}]}


class Env:
    def __init__(self, monkeypatch, checkins=(), qwen_rules=(), store=None, plan_json=PLAN):
        self.conn = connect(":memory:")
        self.conv = insert(self.conn, "conversations", title="c")
        self.task_id = insert(self.conn, "tasks", conversation_id=self.conv, objective="obj", status="queued")
        self.events, self.checkins, self.prompts = [], list(checkins), {}
        self.store = store or FakeStore({})
        self.qwen = ScriptedQwen(*qwen_rules)
        monkeypatch.setattr(task, "QUOTA_RETRY_SECONDS", 0)
        monkeypatch.setattr(events, "publish", lambda conv_id, e: self.events.append(e))
        monkeypatch.setattr(task, "cloud", self.cloud)
        monkeypatch.setattr(task, "PdfStore", lambda: self.store)
        for m in (docs, fields):
            monkeypatch.setattr(m, "qwen", self.qwen)
        for m in (docs, explore):
            monkeypatch.setattr(m, "ddg", lambda q: [])

        async def plan_cloud(role, messages, conn):
            return json.dumps(plan_json)

        monkeypatch.setattr(plan, "cloud", plan_cloud)

    async def cloud(self, role, messages, conn):
        self.prompts.setdefault(role, []).append(messages[-1]["content"])
        if role == "report":
            return "# Report"
        c = self.checkins.pop(0) if self.checkins else {}
        if isinstance(c, Exception):
            raise c
        return json.dumps(c)

    def ctx(self):
        return TaskCtx(self.conn, self.conv, self.task_id, "2026-11-24", self.store)

    def subtask(self, *names):
        sid = insert(self.conn, "subtasks", task_id=self.task_id, objective="s", status="queued")
        for n in names or ("Gross NPA %",):
            insert(self.conn, "fields", task_id=self.task_id, subtask_id=sid, name=n, entity="Navi Finserv",
                   period="FY2025-26", kind="figure", status="open")
        return sid

    def doc(self, pages):
        self.store.docs[AR] = pages
        insert(self.conn, "documents", task_id=self.task_id, key="D1", name="Navi AR", issuer="Navi Finserv",
               status="verified", url=AR, about="Navi AR FY26", covers="financials", pages=len(pages))

    def run_subtask(self, sid):
        asyncio.run(task.run_subtask(self.ctx(), sid))
        return self.conn.execute("select * from subtasks where id=?", (sid,)).fetchone()

    def rounds(self, sid):
        return self.conn.execute("select * from rounds where subtask_id=? order by id", (sid,)).fetchall()

    def notes(self):
        return [e["message"]["content"] for e in self.events if e["type"] == "message"]


def counter():
    n = iter(range(10_000))
    return lambda prompt: {"do": "S", "query": f"alpha{next(n)} beta{next(n)} gamma{next(n)}"}


def test_document_first_fills_fields_and_finishes_done(monkeypatch):
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "accept"}]}],
              qwen_rules=[("Options:", {"do": "D1"}),
                          ("Page 1 of D1", {"found": [{"field": 1, "value": "2.1", "unit": "%", "quote": "GNPA 2.1%"}],
                                            "next": "leave"})])
    env.doc(["GNPA 2.1% at March 2026"])
    sid = env.subtask()
    st = env.run_subtask(sid)
    assert st["status"] == "done" and env.rounds(sid)[0]["query"] == "D1 pp. 1"
    f = env.conn.execute("select * from fields").fetchone()
    assert (f["value"], f["accepted"], f["source_key"], f["page"]) == ("2.1", 1, "D1", 1)


def test_focus_rotates_after_three_stalls(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_EVERY", 99)
    env = Env(monkeypatch, qwen_rules=[("Options:", counter())])
    sid = env.subtask("A", "B")
    env.run_subtask(sid)
    focus = [ln.split(" · ")[0] for p in env.qwen.prompts for ln in p.split("\n") if ln.startswith("Field:")]
    assert focus[:6] == ["Field: A"] * 3 + ["Field: B"] * 3
    assert len(env.prompts["checkin"]) == 1  # both fields stalled -> one check-in, which ends it


def test_step_limit_checkin_reopens_new_loop(monkeypatch):
    monkeypatch.setattr(config, "ROUNDS_PER_LOOP", 2)
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "retry", "hint": "try notes"}]},
                                     {"fields": [{"name": "Gross NPA %", "action": "unavailable", "note": "n/a"}]}],
              qwen_rules=[("Options:", counter())])
    sid = env.subtask()
    st = env.run_subtask(sid)
    assert st["status"] == "done" and st["restarts"] == 1
    assert {r["loop"] for r in env.rounds(sid)} == {0, 1}
    assert any("Hint: try notes" in p for p in env.qwen.prompts)


def test_restart_cap_ends_partial(monkeypatch):
    monkeypatch.setattr(config, "ROUNDS_PER_LOOP", 2)
    monkeypatch.setattr(config, "MAX_RESTARTS", 0)
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "retry"}]}],
              qwen_rules=[("Options:", counter())])
    st = env.run_subtask(env.subtask())
    assert st["status"] == "partial" and st["restarts"] == 0


def test_page_budget_forces_final_checkin(monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES", 1)
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "retry"}]}],
              qwen_rules=[("Options:", {"do": "D1"}), ("Page 1 of D1", {"found": [], "next": "leave"})])
    env.doc(["nothing", "here"])
    st = env.run_subtask(env.subtask())
    assert st["status"] == "partial" and len(env.prompts["checkin"]) == 1


def test_checkin_failure_ends_subtask_partial(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_EVERY", 1)
    err = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    env = Env(monkeypatch, checkins=[err], qwen_rules=[("Options:", counter())])
    assert env.run_subtask(env.subtask())["status"] == "partial"


def test_qwen_not_public_then_confirmed(monkeypatch):
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "unavailable", "note": "private"}]}],
              qwen_rules=[("Options:", {"do": "X", "why": "not disclosed"})])
    sid = env.subtask()
    assert env.run_subtask(sid)["status"] == "done"
    assert env.rounds(sid)[0]["query"] == "Gross NPA %: not public (not disclosed)"
    assert len(env.prompts["checkin"]) == 1  # Qwen's "not public" closes nothing until a check-in confirms it


def test_qwen_not_public_rejected_by_checkin_reopens(monkeypatch):
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "retry", "hint": "see CARE"}]},
                                     {"fields": [{"name": "Gross NPA %", "action": "unavailable"}]}],
              qwen_rules=[("Options:", [{"do": "X", "why": "future"}, {"do": "S", "query": "navi gnpa care"},
                                        {"do": "X", "why": "none"}])])
    sid = env.subtask()
    assert env.run_subtask(sid)["status"] == "done" and len(env.prompts["checkin"]) == 2
    assert any("Today is 2026-11-24" in p for p in env.qwen.prompts)


def test_report_input_marks_unreviewed_and_gaps(monkeypatch):
    env = Env(monkeypatch)
    env.subtask("A", "B")
    env.conn.execute("update fields set status='found', value='2.1', unit='%', source_key='D1', page=4, quote='q' "
                     "where name='A'")
    env.conn.execute("update fields set status='unavailable', note='not disclosed' where name='B'")
    t = dict(env.conn.execute("select * from tasks").fetchone())
    body = task._report_input(env.conn, t, "2026-11-24")
    assert "A (Navi Finserv, FY2025-26): 2.1 %" in body and "unreviewed" in body and "D1 p.4" in body
    assert "Gaps:\n- B (Navi Finserv, FY2025-26): unavailable - not disclosed" in body


def test_run_task_plans_runs_and_reports(monkeypatch):
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "unavailable"}]}],
              qwen_rules=[("Options:", {"do": "X", "why": "none"})])
    assert asyncio.run(task.run_task(env.conn, env.conv, env.task_id)) == "# Report"
    assert "Today: " in env.prompts["report"][0]
    assert env.conn.execute("select status from tasks").fetchone()[0] == "done"


def test_auth_error_fails_task(monkeypatch):
    env = Env(monkeypatch)
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))

    async def bad(role, messages, conn):
        raise openai.AuthenticationError("bad key", response=resp, body=None)

    monkeypatch.setattr(plan, "cloud", bad)
    assert asyncio.run(task.run_task(env.conn, env.conv, env.task_id)) == ""
    assert env.conn.execute("select status from tasks").fetchone()[0] == "failed"
    assert any(".env" in n for n in env.notes())


def test_quota_pause_resumes_and_tells_once(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_EVERY", 1)
    env = Env(monkeypatch, checkins=[QuotaExhausted("checkin"),
                                     {"fields": [{"name": "Gross NPA %", "action": "unavailable"}]}],
              qwen_rules=[("Options:", counter())])
    assert env.run_subtask(env.subtask())["status"] == "done"
    assert "paused_quota" in [e["subtask"]["status"] for e in env.events if e["type"] == "subtask"]
    assert sum("research is paused" in n for n in env.notes()) == 1


def test_cooldown_retry_waits_retry_after(monkeypatch):
    monkeypatch.setattr(config, "CHECKIN_EVERY", 1)
    env = Env(monkeypatch, checkins=[QuotaExhausted("checkin", 60),
                                     {"fields": [{"name": "Gross NPA %", "action": "unavailable"}]}],
              qwen_rules=[("Options:", counter())])
    monkeypatch.setattr(task, "QUOTA_RETRY_SECONDS", 900)
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(task.asyncio, "sleep", fake_sleep)
    env.run_subtask(env.subtask())
    assert slept == [60]


def test_switch_to_groq_is_told_once_per_task(monkeypatch):
    env = Env(monkeypatch, checkins=[{"fields": [{"name": "Gross NPA %", "action": "unavailable"}]}] * 2,
              qwen_rules=[("Options:", {"do": "X", "why": "none"})])
    orig = env.cloud

    async def via_groq(role, messages, conn):
        if note := task.on_fallback.get():
            note(["gemini-3.5-flash-lite"], "qwen/qwen3.8-27b")
        return await orig(role, messages, conn)

    monkeypatch.setattr(task, "cloud", via_groq)
    asyncio.run(task.run_task(env.conn, env.conv, env.task_id))
    assert env.notes().count("Gemini models are facing issues, so I switched to Groq (qwen/qwen3.8-27b).") == 1
