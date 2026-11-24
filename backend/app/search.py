"""Web search (DDG), page fetch, Qwen rerank, and verbatim passage extraction."""
import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

import requests
import trafilatura
from ddgs import DDGS

from app import config
from app.db import Hit
from app.local_llm import WORK, parse_json, qwen
from app.logs import clip

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _norm(s: str) -> str:
    return " ".join(s.split())


def ddg(query: str) -> list[Hit]:
    try:
        rows = DDGS().text(query, max_results=config.SEARCH_MAX_RESULTS, region=config.SEARCH_REGION)
        return [Hit(r.get("title", ""), r["href"], r.get("body", "")) for r in rows]
    except Exception as e:
        log.warning("ddg failed for %r: %r", query, e)
        return []


def fetch(hit: Hit) -> Hit | None:
    t0 = time.monotonic()
    try:
        r = requests.get(hit.url, stream=True, timeout=config.FETCH_TIMEOUT, headers=_HEADERS)
        if r.status_code != 200:
            log.info("fetch skip %s: HTTP %s", hit.url, r.status_code)
            return None
        now = datetime.now(timezone.utc).date().isoformat()
        if "pdf" in r.headers.get("Content-Type", "").lower() or hit.url.lower().endswith(".pdf"):
            log.info("fetch pdf  %s (%.1fs) -> PDF reader", hit.url, time.monotonic() - t0)
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
            log.info("fetch skip %s: no readable text (%d bytes)", hit.url, len(body))
            return None
        log.info("fetch ok   %s: %d chars, published %s (%.1fs)", hit.url, len(data["text"]),
                 data.get("date") or "unknown", time.monotonic() - t0)
        return replace(hit, text=data["text"], published=data.get("date"), fetched_at=now)
    except Exception as e:
        log.info("fetch skip %s: %s after %.1fs", hit.url, type(e).__name__, time.monotonic() - t0)
        return None


async def search(query: str, objective: str, priority: int = WORK) -> list[Hit]:
    log.info("search %r (region %s, max %d)", query, config.SEARCH_REGION, config.SEARCH_MAX_RESULTS)
    found = await asyncio.to_thread(ddg, query)
    log.info("ddg returned %d results", len(found))
    for i, h in enumerate(found):
        log.info("  result %d: %s | %s", i, h.url, clip(h.title, 90))
    fetched = await asyncio.gather(*(asyncio.to_thread(fetch, h) for h in found))
    hits = [h for h in fetched if h]
    log.info("fetched %d/%d readable: %s", len(hits), len(found), ", ".join(h.url for h in hits) or "none")
    if len(hits) < 2:
        log.info("rerank skipped: %d candidate(s)", len(hits))
        return hits
    listing = "\n\n".join(f"[{i}] {h.title}\n{h.url}\n{(h.text or h.snippet)[:800]}" for i, h in enumerate(hits))
    prompt = (f"Objective: {objective}\nQuery: {query}\n\nSources:\n{listing}\n\n"
              'Pick the sources useful for the objective, best first. Reply JSON only: {"keep":[indices]}')
    log.info("rerank: Qwen ranking %d candidates for objective %r", len(hits), clip(objective, 100))
    t0 = time.monotonic()
    raw = await qwen([{"role": "user", "content": prompt}], priority)
    data = parse_json(raw)
    keep = data.get("keep") if data else None
    if not isinstance(keep, list):
        log.warning("rerank reply unparseable after %.1fs, keeping all in original order: %s",
                    time.monotonic() - t0, clip(raw))
        return hits
    seen = []
    for i in keep:
        if isinstance(i, int) and 0 <= i < len(hits) and i not in seen:
            seen.append(i)
    log.info("rerank done in %.1fs: kept %d/%d", time.monotonic() - t0, len(seen), len(hits))
    for rank, i in enumerate(seen, 1):
        log.info("  kept #%d: %s | %s", rank, hits[i].url, clip(hits[i].title, 90))
    for i, h in enumerate(hits):
        if i not in seen:
            log.info("  dropped: %s | %s", h.url, clip(h.title, 90))
    return [hits[i] for i in seen]


async def passages(hit: Hit, objective: str) -> list[str]:
    prompt = (f"Objective: {objective}\n\nText:\n{hit.text[:6000]}\n\n"
              "Copy the passages relevant to the objective EXACTLY as written, no edits. "
              'Reply JSON only: {"passages":["..."]}')
    t0 = time.monotonic()
    raw = await qwen([{"role": "user", "content": prompt}])
    data = parse_json(raw)
    got = data.get("passages") if data else None
    if not isinstance(got, list):
        log.warning("passages %s: reply unparseable (%.1fs): %s", hit.url, time.monotonic() - t0, clip(raw))
        return []
    text = _norm(hit.text)
    proposed = [p for p in map(_norm, (x for x in got if isinstance(x, str))) if p]
    kept = [p for p in proposed if p in text]
    log.info("passages %s: %d proposed, %d verbatim kept, %d rejected as not on page (%.1fs)",
             hit.url, len(proposed), len(kept), len(proposed) - len(kept), time.monotonic() - t0)
    for p in proposed:
        log.debug("  %s %s", "kept    " if p in kept else "rejected", clip(p, 200))
    return kept
