"""Web search (DDG), page fetch, Qwen rerank, and verbatim passage extraction."""
import asyncio
import json
import re
import logging
import time
from dataclasses import replace
from urllib.parse import urlparse
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


_STOP = {"limited", "ltd", "pvt", "private", "inc", "corp", "corporation", "company", "co", "plc", "llp",
         "the", "and", "of"}


def name_words(name: str) -> set[str]:
    """Distinctive words of an entity name: 'Navi Finserv Limited' -> {'navi', 'finserv'}."""
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3 and w not in _STOP}


def _seen_text(hit: Hit) -> str:
    return f"{hit.url} {hit.title} {hit.snippet}".lower()


def mentions(hit: Hit, words: set[str]) -> bool:
    """Free screen: does the URL, title or snippet name the entity?"""
    t = _seen_text(hit)
    return any(w in t for w in words)


def _years(period: str) -> set[str]:
    """'FY2023-24' -> {'2023', '2024', '23-24', 'fy24'}-style tokens that may show in a URL or title."""
    p = (period or "").lower()
    ys = set(re.findall(r"20\d\d", p))
    for a, b in re.findall(r"(20\d\d)\s*[-/]\s*(\d\d)", p):
        ys |= {"20" + b, f"{a[2:]}-{b}", f"fy{b}", f"fy{a[2:]}"}
    ys |= {f"fy{y[2:]}" for y in list(ys) if len(y) == 4}
    return ys


def doc_score(hit: Hit, words: set[str], period: str, domains: list[str]) -> int:
    t, ys = _seen_text(hit), _years(period)
    score = 2 * sum(w in t for w in words)
    if any(y in t for y in ys):
        score += 2
    elif any(y not in ys for y in re.findall(r"20\d\d", f"{hit.url} {hit.title}")):
        score -= 2  # names another year and not ours
    host = urlparse(hit.url).netloc.lower()
    if any(host == d or host.endswith("." + d) for d in domains):
        score += 1
    return score


def order_by_entity(hits: list[Hit], entity: str) -> list[Hit]:
    """Hits naming the entity first; never drops any (topic entities like 'India' must still work)."""
    words = name_words(entity)
    return sorted(hits, key=lambda h: not mentions(h, words)) if words else list(hits)


def window(text: str, keywords: list[str], n: int = 6000) -> str:
    """Up to n chars of a long page, starting a little before the first keyword hit."""
    low = text.lower()
    hits = [i for k in keywords if k and (i := low.find(k.lower())) >= 0]
    start = max(0, min(hits) - n // 4) if hits else 0
    return text[start:][:n]


def url_key(url: str) -> str:
    return url.split("#")[0].rstrip("/")


async def search(query: str, objective: str, priority: int = WORK, seen: set[str] | None = None) -> list[Hit]:
    """seen: URL keys already handled by the caller; those results are skipped and new ones are added."""
    log.info("search %r (region %s, max %d)", query, config.SEARCH_REGION, config.SEARCH_MAX_RESULTS)
    found = await asyncio.to_thread(ddg, query)
    log.info("ddg returned %d results", len(found))
    for i, h in enumerate(found):
        log.info("  result %d: %s | %s", i, h.url, clip(h.title, 90))
    if seen is not None:
        fresh = []
        for h in found:
            if url_key(h.url) in seen:
                log.info("  skip (already read): %s", h.url)
            else:
                seen.add(url_key(h.url))
                fresh.append(h)
        log.info("%d new result(s) after skipping already-read URLs", len(fresh))
        found = fresh
    fetched = await asyncio.gather(*(asyncio.to_thread(fetch, h) for h in found))
    hits = [h for h in fetched if h]
    log.info("fetched %d/%d readable: %s", len(hits), len(found), ", ".join(h.url for h in hits) or "none")
    if len(hits) < 2:
        log.info("rerank skipped: %d candidate(s)", len(hits))
        return hits
    listing = "\n\n".join(f"[{i}]{' [PDF]' if h.is_pdf else ''} {h.title}\n{h.url}\n{(h.text or h.snippet)[:800]}"
                          for i, h in enumerate(hits))
    prompt = (f"Objective: {objective}\nQuery: {query}\n\nSources:\n{listing}\n\n"
              "Pick the sources useful for the objective, best first. If the objective needs figures, rank "
              "official PDF documents (annual reports, filings) first. "
              'Reply JSON only: {"keep":[indices]}')
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
