# Handoff — deep research harness (for the next agent)

Written 2026-11-25, updated 2026-11-26. Read this, then `README.md` (its roadmap checklist is the to-do list), then
`docs/eval/2026-11-26-mtplx-speed.md` and the latest spec (`docs/superpowers/specs/2026-11-25-specialist-agents-design.md`).

## What this is

A **harness for a small local model** to do research on its own: web search, opening pages, downloading PDFs,
turning them into text (PDF text layer; indra-ocr for page reads), searching inside them, and separate tool loops
for separate jobs. A larger cloud model plans the checklist of **fields** (figure or fact + entity + period), gives
one final review and writes the cited report. **General purpose**: finance requests are only test cases because
they are easy to verify.

## State of the repo

- `feat/bonsai-harness` was merged into `main` and pushed to GitHub (`SparkyMSV/deepResearch`) on 2026-11-26, at the
  user's request. Merge or push again **only when the user asks**.
- Backend tests: `cd backend && perl -e 'alarm 300; exec @ARGV' python -m pytest -q` → **213 passed** (macOS has
  no `timeout`; always read the summary line, never a filtered tail). Frontend: `cd frontend && npx vitest run`.
- Untracked `package-lock.json` at the repo root and `notebooks/`: leave them alone.

## Current model setup (decided 2026-11-25)

**Research model = Qwen 3.5 4B** (`Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed`) on MTPLX port **8002**, context
window **32768**. Bonsai 2 27B (port 8001) was tested on the same harness and dropped: ~53 s per tool turn vs ~5 s
for the 4B, and memory pauses with both loaded. The user may later try Bonsai 2.5; no code change is needed for a
model swap (the harness reads the model id from the server).

Settings for the 4B (the defaults in `config.py` and `.env.example` since 2026-11-26):

```
DR_RESEARCH_URL=http://127.0.0.1:8002/v1
DR_CONTEXT_TOKENS=28000   # compaction threshold
DR_PDF_STEPS=20
DR_WEB_STEPS=20
DR_THINK_TOKENS=1200      # planning-call thinking budget
```

Start the model server with the SSD session cache **off** (it wrote ~23 GB/hour; the user had it cleared) and the
**`turbo` profile** (10-35% faster decode than the app's default `sustained`; see the speed report):

```
~/.mtplx/bin/mtplx serve --model Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed --host 127.0.0.1 --port 8002 \
  --no-auth --context-window 32768 --ssd-session-cache off --profile turbo
```

The user runs MTPLX from the MTPLX app; don't kill their servers. Ask them to restart one when needed.

## How to test (scratch backend, never the user's)

The user's backend is on **8765** with `data/dr.sqlite` — don't touch either. Use a scratch backend on **8766** with
its own DB and log under the session scratchpad:

```
DR_RESEARCH_URL=http://127.0.0.1:8002/v1 DR_CONTEXT_TOKENS=28000 DR_PDF_STEPS=20 DR_WEB_STEPS=20 DR_THINK_TOKENS=1200 \
DR_DB=$S/run.sqlite DR_LOG_FILE=$S/run.log \
uvicorn app.main:app --app-dir /Users/venu/Desktop/NB/deepResearch/backend --host 127.0.0.1 --port 8766
# then: POST /api/conversations ; POST /api/conversations/{id}/messages {"text":"task: ..."}
```

Use an absolute `--app-dir` (a relative one failed once in a background shell). Watch the log with a filter on
`plan: |tool |recorded '|agent on|self-review|final review|task N:`; every tool call is logged with its args.

Known-answer tests (both pass on the 4B):

| Test | Expected | Last result |
|---|---|---|
| `task: what was the interest coverage ratio of Mindspace Business Parks REIT for FY2025-26` | 3.17x, AR FY2025-26 printed p.526–527, consolidated ratios table, row "Interest service coverage ratio" (standalone is 2.38x) | 3/3 fields, 3 min 24 s |
| `task: in the SURMOUNT-1 trial, what was the mean percentage weight reduction at 72 weeks with tirzepatide 15 mg, and with placebo` | −20.9% / −3.1% | 2/2 fields, 2 min 16 s |

## Architecture (backend/app)

- `task.py` — research queue (one at a time), plan → find documents → sub-tasks → identity checks → final review → report.
- `plan.py` — cloud planner: complete-answer checklist sized to the request, fields grouped by source.
- `docs.py` — find planned documents: searches, **the model picks candidates** (`_choose`), verifies (entity itself,
  not a related company; records `dated`), prepares them in the background.
- `docprep.py` — contents (any layout), per-section printed↔PDF page mapping incl. **two-page spreads**, placement
  checked against running-header page numbers, BM25 page index (`locate`), notes index (`list_notes`).
- `agentkit.py` — the shared tool loop: max 3 calls/turn, nudges, compaction, memory pauses, and a **separate
  planning call** (thinking on, no tools) before the first turn and after a failed attempt — models don't think
  when tools are offered.
- `agent.py` — main agent (delegates to PDF/web agents, not-public guard) + self-review.
- `pdf_agent.py` — `locate`, `list_notes`, `read_pages`, `record_value`, `report`. Recording accepts a row label +
  number (from `row`, `column` or `quote`) and checks it on **any page read in the call**.
- `tables.py` — reads indra-ocr HTML tables (colspan, serial-number columns, split headers).
- `web_agent.py` — search (engines' own order), open, record, with research-wide search history and remarks.
- `final.py`, `fields.py`, `local_llm.py`, `cloud_llm.py`, `pdf.py`, `db.py` (adds missing columns to old DBs).

## Principles the user set (follow them)

- **Harness, not workflow**: code gives tools, information and guardrails (exact quotes, budgets, page mapping, table
  parsing); the **model makes the research choices**. No deterministic reranking or filtering of candidates.
- Domain-neutral prompts; agents are researchers (source-first, narrow down: source → section → note/table).
- Reviews judge substance (number, entity, period, item, quote support), never format (sign, rounding, units).
- Build it yourself (TDD, failing test first), then a fresh-reviewer pass and a verified sharp-edges list.
- Keep SSD writes minimal.

## Open items (in rough priority)

The full list is the **roadmap checklist at the end of `README.md`**. The first ones to do:

1. **Planner outputs the report structure first** (agreed with the user, not built). The plan call lists the final
   report's sections in the JSON before the fields, then the fields each section needs. It's one call and
   automatic (no approval step). Store the sections (a structure column on tasks, a `section` label on fields) and
   pass the same outline to the report writer. Fields keep their **source-based** sub-tasks; don't regroup by
   section. Watch the 40-field cap leaving sections empty. Touches `plan.py`, `db.py`, `task.py`
   (`REPORT_PROMPT`), plan tests.
2. **Speed:** thinking-only planning calls are 28% of model time (32 calls averaging 19 s, empty answers);
   no-tool text replies are another 28%; web searches take 15-30 s each. See `docs/eval/2026-11-26-mtplx-speed.md`.
3. **Self-review quality** (from the NABKISAN live run): it rejected NABARD's 89.12% for a wrong reason and accepted
   total borrowings as "bonds outstanding".
4. New file abilities the user wants later: **XLSX reading, XLSX writing, PDF writing** (report export).
5. Report cites the planned document name instead of what verification found (`about`); ₹ rendered as a backtick.
6. Measure SSD writes per minute with the SSD cache off (sampler: per-process `proc_pid_rusage` bytes written +
   `ioreg` disk total).
7. Parked by the user: reviewers flagging second-hand sources; a LoRA fine-tune of the 4B on its own accepted
   traces (not on Parallel AI outputs: their terms forbid it).
8. The user stopped their MTPLX server on 8002 on 2026-11-26. Start it with the command above when needed. Their
   backend on 8765 runs old code until restarted.

## Gotchas

- Web search engines rate-limit after heavy testing (timeouts, "no results"); it's external.
- With thinking on, the 4B can put everything in its reasoning and leave the answer empty; the harness falls back
  to the reasoning (plans) or retries without thinking (self-review). Keep that behaviour.
- MTPLX answers 507 when memory is short; the harness pauses, it doesn't fail.
