"""After all sub-tasks: consistency checks (only when every figure of an identity was found) and one cloud
final review of every value."""
import logging
import re

import openai

from app.fields import cite
from app.ctx import date_note
from app.local_llm import parse_json
from app.logs import clip

log = logging.getLogger(__name__)

TOLERANCE = 0.01
_UNITS = {"crore": 1e7, "cr": 1e7, "lakh": 1e5, "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9,
          "thousand": 1e3}
# identity: (name, [(member, aliases, sign)]) — sum of signed members must be ~0
IDENTITIES = [
    ("assets = liabilities + equity", [
        ("total assets", ("total assets",), 1),
        ("total liabilities", ("total liabilities",), -1),
        ("equity", ("equity", "total equity", "net worth", "shareholders' equity", "shareholders equity"), -1)]),
    ("net profit = revenue − expenses − tax", [
        ("revenue", ("total revenue", "total income"), 1),  # revenue from operations excludes other income
        ("expenses", ("total expenses", "expenses"), -1),
        ("tax", ("tax expense", "total tax expense", "tax"), -1),
        ("net profit", ("net profit", "profit after tax", "pat"), -1)]),
    ("net NPA = gross NPA − provisions", [
        ("gross npa", ("gross npa", "gross non-performing assets"), 1),
        ("provisions", ("provisions", "npa provisions", "provision"), -1),
        ("net npa", ("net npa", "net non-performing assets"), -1)]),
]


def _base(name: str) -> str:
    """'Profit After Tax (PAT)' -> 'profit after tax'; 'Gross NPA %' -> 'gross npa'."""
    text = re.sub(r"\(.*?\)", " ", (name or "").lower())
    return " ".join(re.sub(r"[^a-z' &-]", " ", text).split())


def _amount(value: str, unit: str) -> float | None:
    """A rupee amount in units of one, or None (percentages, ratios, unparseable values)."""
    text = f"{value} {unit}".lower()
    if "%" in text or "percent" in text or "times" in text or "x" == (unit or "").strip().lower():
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    if not m:
        return None
    n = float(m.group().replace(",", ""))
    if re.search(r"\(\s*" + re.escape(m.group()), str(value)):  # (1,234) is a loss / negative figure
        n = -n
    return n * next((f for u, f in _UNITS.items() if re.search(rf"\b{u}s?\b", text)), 1)


def identity_notes(fields: list[dict]) -> dict[int, str]:
    """{field id: note} for identities whose every member was found for the same entity and period and that
    don't add up within 1%. Nothing is rejected; the note goes to the final review and the report."""
    notes: dict[int, str] = {}
    groups: dict[tuple, list[dict]] = {}
    for f in fields:
        if f["status"] == "found" and f["value"]:
            groups.setdefault(((f["entity"] or "").lower(), (f["period"] or "").lower()), []).append(f)
    for rows in groups.values():
        for title, members in IDENTITIES:
            picked = []
            for _, aliases, sign in members:
                hit = next((f for f in rows if _base(f["name"]) in aliases), None)
                amt = _amount(hit["value"], hit["unit"] or "") if hit else None
                if amt is None:
                    break
                picked.append((hit, amt, sign))
            else:
                total = sum(a * s for _, a, s in picked)
                scale = max(abs(a) for _, a, _ in picked) or 1
                if abs(total) > TOLERANCE * scale:
                    shown = ", ".join(f"{h['name']} {h['value']} {h['unit'] or ''}".strip() for h, _, _ in picked)
                    for h, _, _ in picked:
                        notes[h["id"]] = f"doesn't add up ({title}): {shown}"
    return notes


FINAL_PROMPT = (
    "You give the final check before a research report is written. For each numbered field decide: accept (the "
    "value is for the field's entity and period and its quote supports it; or the research's 'not public' finding "
    "is believable) or reject (wrong or unsupported; say why). Judge only from the values, quotes and notes shown, "
    "not from your own beliefs about the entity or whether it exists. Notes saying figures don't add up deserve a "
    "close look. Reply with JSON only: "
    '{"fields":[{"field":1,"action":"accept","reason":""}]}'
)


async def final_review(ctx, ask) -> None:
    """One cloud call over every field of the research. ask(messages) -> reply text. On any failure the
    values keep their review levels (the report labels them)."""
    rows = [dict(r) for r in ctx.conn.execute("select * from fields where task_id=? order by id", (ctx.task_id,))]
    lines = []
    for i, f in enumerate(rows, 1):
        head = f"[{i}] {f['name']} — {f['entity'] or 'any'}, {f['period']}: "
        if f["status"] == "found":
            src = cite(dict(f))
            body = f"{f['value']} {f['unit'] or ''} ({src}) quote: \"{f['quote']}\""
        else:
            body = "not public" if f["status"] == "unavailable" else "not found"
        lines.append(head + body + (f" | note: {f['note']}" if f["note"] else ""))
    msgs = [{"role": "system", "content": FINAL_PROMPT},
            {"role": "user", "content": date_note(ctx.today) + "\n" + "\n".join(lines)}]
    try:
        d = parse_json(await ask(msgs) or "")
    except openai.APIError as e:
        log.warning("final review failed: %r; values keep their review levels", e)
        return
    items = d.get("fields") if isinstance(d, dict) else None
    if not isinstance(items, list):
        log.warning("final review reply unusable; values keep their review levels")
        return
    for a in items:
        n = a.get("field") if isinstance(a, dict) else None
        n = int(n) if isinstance(n, str) and n.isdigit() else n
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(rows):
            continue
        f, act, why = rows[n - 1], a.get("action"), str(a.get("reason") or "").strip()
        if act == "accept" and f["status"] in ("found", "unavailable"):
            ctx.set_field(f, accepted=2)
        elif act == "reject":
            ctx.set_field(f, status="open", accepted=0, value=None, unit=None, reported_period=None, source_url=None,
                          source_key=None, page=None, quote=None, source_kind=None,
                          note=f"rejected in final review: {why}" if why else "rejected in final review")
    log.info("final review: %s", clip(items, 300))
