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
    monkeypatch.setenv("DR_ROUNDS_PER_LOOP", "10")
    monkeypatch.setenv("DR_MAX_RESTARTS", "1")
    c = reload_config()
    assert (c.MAX_SUBTASKS, c.ROUNDS_PER_LOOP, c.MAX_RESTARTS) == (3, 10, 1)
    for bad in ("abc", "0"):
        monkeypatch.setenv("DR_MAX_SUBTASKS", bad)
        assert reload_config().MAX_SUBTASKS == 8
