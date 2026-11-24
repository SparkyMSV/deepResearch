"""Web search (DDG), page fetch, Qwen rerank, and verbatim passage extraction."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import requests
import trafilatura
from ddgs import DDGS

from app import config
from app.db import Hit
from app.local_llm import WORK, parse_json, qwen

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _norm(s: str) -> str:
    return " ".join(s.split())


def ddg(query: str) -> list[Hit]:
    try:
        rows = DDGS().text(query, max_results=config.SEARCH_MAX_RESULTS, region=config.SEARCH_REGION)
        return [Hit(r.get("title", ""), r["href"], r.get("body", "")) for r in rows]
    except Exception:
        return []


def fetch(hit: Hit) -> Hit | None:
    try:
        r = requests.get(hit.url, stream=True, timeout=config.FETCH_TIMEOUT, headers=_HEADERS)
        if r.status_code != 200:
            return None
        now = datetime.now(timezone.utc).date().isoformat()
        if "pdf" in r.headers.get("Content-Type", "").lower() or hit.url.lower().endswith(".pdf"):
            return replace(hit, is_pdf=True, fetched_at=now)
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) >= config.FETCH_MAX_BYTES:
                body = body[:config.FETCH_MAX_BYTES]
                break
        out = trafilatura.extract(body, with_metadata=True, output_format="json")
        data = json.loads(out) if out else {}
        if not data.get("text"):
            return None
        return replace(hit, text=data["text"], published=data.get("date"), fetched_at=now)
    except Exception:
        return None


async def search(query: str, objective: str, priority: int = WORK) -> list[Hit]:
    found = await asyncio.to_thread(ddg, query)
    fetched = await asyncio.gather(*(asyncio.to_thread(fetch, h) for h in found))
    hits = [h for h in fetched if h]
    if len(hits) < 2:
        return hits
    listing = "\n\n".join(f"[{i}] {h.title}\n{h.url}\n{(h.text or h.snippet)[:800]}" for i, h in enumerate(hits))
    prompt = (f"Objective: {objective}\nQuery: {query}\n\nSources:\n{listing}\n\n"
              'Pick the sources useful for the objective, best first. Reply JSON only: {"keep":[indices]}')
    data = parse_json(await qwen([{"role": "user", "content": prompt}], priority))
    keep = data.get("keep") if data else None
    if not isinstance(keep, list):
        return hits
    seen = []
    for i in keep:
        if isinstance(i, int) and 0 <= i < len(hits) and i not in seen:
            seen.append(i)
    return [hits[i] for i in seen]


async def passages(hit: Hit, objective: str) -> list[str]:
    prompt = (f"Objective: {objective}\n\nText:\n{hit.text[:6000]}\n\n"
              "Copy the passages relevant to the objective EXACTLY as written, no edits. "
              'Reply JSON only: {"passages":["..."]}')
    data = parse_json(await qwen([{"role": "user", "content": prompt}]))
    got = data.get("passages") if data else None
    if not isinstance(got, list):
        return []
    text = _norm(hit.text)
    return [p for p in map(_norm, (x for x in got if isinstance(x, str))) if p and p in text]
