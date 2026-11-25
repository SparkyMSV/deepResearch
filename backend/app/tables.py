"""Read one figure from the HTML tables indra-ocr writes, by row label and column heading, so the agent names
where a number is instead of copying it."""
import re
from typing import NamedTuple

from app.pdf import norm

_TABLE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_NUM = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?%?$")
_WORD = re.compile(r"[a-z0-9]+")


class Cell(NamedTuple):
    value: str = ""
    row: str = ""
    column: str = ""
    quote: str = ""
    error: str = ""


def _parse(page: str) -> list[tuple[list[str], list[list[str]]]]:
    """(column headings, data rows) per table. Header rows are those before the first row holding a number;
    a column's heading joins its header cells ("As at" + "March 31, 2026").
    shortcut: colspan/rowspan are ignored; upgrade if OCR tables with merged cells mis-assign columns."""
    out = []
    for t in _TABLE.findall(page):
        rows = [[norm(c) for c in _CELL.findall(r)] for r in _ROW.findall(t)]
        rows = [r for r in rows if r]
        first = next((i for i, r in enumerate(rows) if any(_NUM.match(c) for c in r[1:])), len(rows))
        width = max(map(len, rows), default=0)
        heads = [" ".join(r[j] for r in rows[:first] if j < len(r) and r[j]) for j in range(width)]
        out.append((heads, rows[first:]))
    return out


def _words(s: str) -> set[str]:
    return set(_WORD.findall(s.lower()))


def cell(page: str, row: str, column: str) -> Cell:
    tabs = _parse(page)
    if not tabs:
        return Cell(error="no table on this page; record with a quote instead")
    want = norm(row).lower()
    rows = [(heads, r) for heads, rs in tabs for r in rs if r and r[0]]
    exact = [x for x in rows if x[1][0].lower() == want]
    hits = exact or [x for x in rows if want in x[1][0].lower()]
    if not hits:
        return Cell(error="no row labelled like that; rows: " + "; ".join(r[0] for _, r in rows[:12]))
    if len({r[0] for _, r in hits}) > 1:
        return Cell(error="several rows match; use the full label: " + "; ".join(r[0] for _, r in hits[:8]))
    heads, r = hits[0]
    q = _words(column)
    scores = [len(q & _words(h)) if j else 0 for j, h in enumerate(heads)]  # column 0 holds the labels
    best = max(scores, default=0)
    cols = [j for j, s in enumerate(scores) if s == best and s]
    if len(cols) != 1:
        return Cell(error="say which column: " + "; ".join(h for h in heads[1:] if h))
    j = cols[0]
    if j >= len(r) or not r[j]:
        return Cell(error=f"the row {r[0]!r} has no value under {heads[j]!r}")
    return Cell(value=r[j], row=r[0], column=heads[j], quote=f"{r[0]} ... {r[j]}")
