import asyncio
import logging

from app import logs, search
from app.db import Hit


def test_context_ids_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DR_LOG_FILE", str(tmp_path / "dr.log"))
    logs.setup(force=True)
    try:
        logs.conv.set(3); logs.task.set(7); logs.sub.set(21)
        logging.getLogger("app.test").info("hello")
        for h in logging.getLogger("app").handlers:
            h.flush()
        line = (tmp_path / "dr.log").read_text()
        assert "[c3 t7 s21]" in line and "hello" in line
    finally:
        logs.conv.set(None); logs.task.set(None); logs.sub.set(None)
        logs.teardown()


def test_search_logs_every_url_and_rerank(monkeypatch, caplog):
    hs = [Hit("A", "http://a", "s"), Hit("B", "http://b", "s"), Hit("C", "http://c", "s")]
    monkeypatch.setattr(search, "ddg", lambda q: hs)
    monkeypatch.setattr(search, "fetch", lambda h: None if h.url == "http://c" else h)

    async def fake_qwen(messages, priority=1, max_tokens=800):
        return '{"keep":[1]}'

    monkeypatch.setattr(search, "qwen", fake_qwen)
    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(search.search("q", "obj"))
    text = caplog.text
    assert all(u in text for u in ("http://a", "http://b", "http://c"))
    assert "rerank" in text and "kept" in text and "dropped" in text
