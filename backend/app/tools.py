"""Shared tool helpers: the near-duplicate search check (80% shared words)."""
import re


def qkey(q: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", q.lower()))


def fresh(q, queries: list[str]) -> str | None:
    """q stripped, or None if it's empty or shares 80%+ of its words with a query already tried."""
    if not isinstance(q, str) or not q.strip():
        return None
    k = qkey(q)
    return None if any(len(k & qkey(x)) >= 0.8 * len(k | qkey(x)) for x in queries) else q.strip()

