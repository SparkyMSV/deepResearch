import asyncio
import json

import pytest

from app import search
from app.db import Hit


class Resp:
    def __init__(self, body=b"", status=200, ctype="text/html"):
        self.status_code, self.headers, self._b = status, {"Content-Type": ctype}, body

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]


def fake_get(resp):
    return lambda *a, **k: resp


def test_ddg_error_returns_empty(monkeypatch):
    class Boom:
        def text(self, *a, **k):
            raise RuntimeError("x")

    monkeypatch.setattr(search, "DDGS", Boom)
    assert search.ddg("q") == []


def test_ddg_maps_fields(monkeypatch):
    class D:
        def text(self, q, max_results):
            return [{"title": "T", "href": "http://a", "body": "B"}]

    monkeypatch.setattr(search, "DDGS", D)
    assert search.ddg("q") == [Hit("T", "http://a", "B")]


def test_fetch_skips_non_200_and_timeout(monkeypatch):
    h = Hit("t", "http://a", "s")
    monkeypatch.setattr(search.requests, "get", fake_get(Resp(status=404)))
    assert search.fetch(h) is None

    def boom(*a, **k):
        raise TimeoutError

    monkeypatch.setattr(search.requests, "get", boom)
    assert search.fetch(h) is None


def test_fetch_html(monkeypatch):
    monkeypatch.setattr(search.requests, "get", fake_get(Resp(b"<html>x</html>")))
    monkeypatch.setattr(search.trafilatura, "extract", lambda *a, **k: json.dumps({"text": "hello", "date": "2024-01-02"}))
    r = search.fetch(Hit("t", "http://a", "s"))
    assert r.text == "hello" and r.published == "2024-01-02" and r.fetched_at


def test_fetch_empty_text_is_none(monkeypatch):
    monkeypatch.setattr(search.requests, "get", fake_get(Resp(b"<html/>")))
    monkeypatch.setattr(search.trafilatura, "extract", lambda *a, **k: None)
    assert search.fetch(Hit("t", "http://a", "s")) is None


def test_fetch_marks_pdf(monkeypatch):
    monkeypatch.setattr(search.requests, "get", fake_get(Resp(b"%PDF", ctype="application/pdf")))
    r = search.fetch(Hit("t", "http://a/x", "s"))
    assert r.is_pdf and r.text == ""


def test_fetch_truncates_large(monkeypatch):
    seen = []
    monkeypatch.setattr(search.requests, "get", fake_get(Resp(b"a" * 5_000_000)))
    monkeypatch.setattr(search.trafilatura, "extract",
                        lambda html, **k: seen.append(len(html)) or json.dumps({"text": html[:10]}))
    assert search.fetch(Hit("t", "http://a", "s")) is not None
    assert seen[0] <= 2_000_000


def hits(n=3):
    return [Hit(f"t{i}", f"http://{i}", "s", text=f"text{i}") for i in range(n)]


def run_search(monkeypatch, reply):
    hs = hits()

    async def fake_qwen(messages, priority=1, max_tokens=800):
        return reply

    monkeypatch.setattr(search, "ddg", lambda q: hs)
    monkeypatch.setattr(search, "fetch", lambda h: h)
    monkeypatch.setattr(search, "qwen", fake_qwen)
    return hs, asyncio.run(search.search("q", "obj"))


def test_rerank_order(monkeypatch):
    hs, out = run_search(monkeypatch, '{"keep":[2,0]}')
    assert out == [hs[2], hs[0]]


def test_rerank_bad_json_keeps_all(monkeypatch):
    hs, out = run_search(monkeypatch, "nonsense")
    assert out == hs


def test_passages_must_be_verbatim(monkeypatch):
    h = Hit("t", "u", "s", text="The quick  brown\nfox jumps over the dog.")

    async def fake_qwen(messages, priority=1, max_tokens=800):
        return json.dumps({"passages": ["quick brown fox jumps", "invented sentence"]})

    monkeypatch.setattr(search, "qwen", fake_qwen)
    assert asyncio.run(search.passages(h, "obj")) == ["quick brown fox jumps"]

    async def bad(messages, priority=1, max_tokens=800):
        return "nope"

    monkeypatch.setattr(search, "qwen", bad)
    assert asyncio.run(search.passages(h, "obj")) == []
