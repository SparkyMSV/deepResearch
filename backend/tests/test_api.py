import pytest
from fastapi.testclient import TestClient

from app import config, db


@pytest.fixture
def dbpath(tmp_path, monkeypatch):
    p = str(tmp_path / "t.sqlite")
    monkeypatch.setattr(config, "DB_PATH", p)
    return p


@pytest.fixture
def client(dbpath, monkeypatch):
    import app.main as main

    async def stub(conn, conv_id, text):  # like the real one: stores the user message
        db.insert(conn, "messages", conversation_id=conv_id, role="user", content=text, kind="text")

    monkeypatch.setattr(main, "handle_message", stub)
    with TestClient(main.app) as c:
        yield c


def test_login_ok_and_bad(client):
    ok = client.post("/api/login", json={"email": config.DEMO_USER, "password": config.DEMO_PASSWORD})
    assert ok.status_code == 200 and ok.json() == {"token": "demo"}
    assert client.post("/api/login", json={"email": "x", "password": "y"}).status_code == 401


def test_empty_message_400(client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": "  "}).status_code == 400


def test_conversation_roundtrip(client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": "hello"}).status_code == 202
    body = client.get(f"/api/conversations/{cid}").json()
    assert [m["content"] for m in body["messages"]] == ["hello"]
    assert body["messages"][0]["role"] == "user" and body["tasks"] == []
    assert [c["id"] for c in client.get("/api/conversations").json()] == [cid]
    assert client.get("/api/conversations/999").status_code == 404


def test_task_tree_in_detail(client, dbpath):
    cid = client.post("/api/conversations").json()["id"]
    conn = db.connect(dbpath)
    t = db.insert(conn, "tasks", conversation_id=cid, objective="o", status="running")
    s = db.insert(conn, "subtasks", task_id=t, objective="s", status="running")
    db.insert(conn, "rounds", subtask_id=s, loop=0, n=1, query="q", hits=3)
    body = client.get(f"/api/conversations/{cid}").json()
    assert body["tasks"][0]["subtasks"][0]["rounds"][0]["hits"] == 3


def test_startup_marks_interrupted(dbpath, monkeypatch):
    import app.main as main

    conn = db.connect(dbpath)
    t = db.insert(conn, "tasks", conversation_id=1, objective="o", status="running")
    s = db.insert(conn, "subtasks", task_id=t, objective="s", status="queued")
    conn.close()
    with TestClient(main.app):
        pass
    conn = db.connect(dbpath)
    assert conn.execute("select status from subtasks where id=?", (s,)).fetchone()[0] == "interrupted"
    assert conn.execute("select status from tasks where id=?", (t,)).fetchone()[0] == "interrupted"
