import importlib

import pytest

from app import config
from app.db import connect, insert, update

TABLES = {"conversations", "messages", "tasks", "subtasks", "rounds", "evidence", "quota"}


def test_schema_created():
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert TABLES <= names


def test_insert_update():
    conn = connect(":memory:")
    cid = insert(conn, "conversations", title="t")
    tid = insert(conn, "tasks", conversation_id=cid, objective="o", status="queued")
    assert isinstance(tid, int)
    update(conn, "tasks", tid, status="done")
    assert conn.execute("select status from tasks where id=?", (tid,)).fetchone()["status"] == "done"


def test_config_never_gpt_oss():
    assert all("gpt-oss" not in m for ms in config.ROLES.values() for m in ms)


def test_roles_order():
    assert config.ROLES["checkin"] == ["qwen/qwen3.8-27b", "gemini-3.1-flash-lite"]


@pytest.fixture
def reload_config(monkeypatch):
    yield lambda: importlib.reload(config)
    monkeypatch.undo()
    importlib.reload(config)


def test_limits_from_env(monkeypatch, reload_config):
    monkeypatch.setenv("DR_MAX_SUBTASKS", "3")
    monkeypatch.setenv("DR_MAX_DELEGATIONS", "10")
    monkeypatch.setenv("DR_MAX_RESTARTS", "1")
    c = reload_config()
    assert (c.MAX_SUBTASKS, c.MAX_DELEGATIONS, c.MAX_RESTARTS) == (3, 10, 1)
    for bad in ("abc", "0"):
        monkeypatch.setenv("DR_MAX_SUBTASKS", bad)
        assert reload_config().MAX_SUBTASKS == 8


def test_new_tables_exist():
    conn = connect(":memory:")
    d = insert(conn, "documents", task_id=1, key="D1", name="AR", status="planned", domains='["navi.com"]')
    f = insert(conn, "fields", task_id=1, subtask_id=2, name="Gross NPA %", status="open", sources='["D1"]')
    assert conn.execute("select key from documents where id=?", (d,)).fetchone()[0] == "D1"
    row = conn.execute("select * from fields where id=?", (f,)).fetchone()
    assert row["accepted"] == 0 and row["reported_period"] is None


def test_new_columns():
    conn = connect(":memory:")
    d = insert(conn, "documents", task_id=1, key="D1", name="AR", status="verified", offset_ok=1, contents="[]", notes="x")
    f = insert(conn, "fields", task_id=1, subtask_id=1, name="n", status="open", printed_page=150)
    assert conn.execute("select offset_ok from documents where id=?", (d,)).fetchone()[0] == 1
    assert conn.execute("select printed_page from fields where id=?", (f,)).fetchone()[0] == 150


def test_settings_specialists():
    assert (config.MAX_DELEGATIONS, config.PDF_STEPS, config.WEB_STEPS) == (6, 20, 20)


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("create table documents(id integer primary key autoincrement, task_id integer, key text)")
    old.execute("insert into documents(task_id, key) values(1, 'D1')")
    old.commit()
    old.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("pragma table_info(documents)")}
    assert {"offset_ok", "contents", "notes", "url"} <= cols
    update(conn, "documents", 1, offset_ok=1)
    row = conn.execute("select key, offset_ok from documents").fetchone()
    assert (row["key"], row["offset_ok"]) == ("D1", 1)


def test_old_table_gets_a_column_with_a_non_constant_default(tmp_path):
    import sqlite3
    path = str(tmp_path / "old2.sqlite")
    old = sqlite3.connect(path)
    old.execute("create table messages(id integer primary key autoincrement, content text)")
    old.execute("insert into messages(content) values('hi')")
    old.commit()
    old.close()
    conn = connect(path)
    assert "created_at" in {r["name"] for r in conn.execute("pragma table_info(messages)")}
    assert conn.execute("select content from messages").fetchone()["content"] == "hi"
