# Deep Research Harness

A **harness** that lets a small **local model** do research on its own machine. The model makes the research
choices: what to search, which result or document to open, which pages to read, what to record. The harness
gives it tools, information and guardrails: web search, page opening, PDF download and text extraction,
search inside documents, separate tool loops for separate jobs, and checks that every recorded value is
quoted word for word from its source.

A research run fills a checklist of **fields**: a figure or short fact for an entity and period, e.g.
"Net Debt, Mindspace REIT, FY2025-26". Each value comes with a source and an exact quote, and the run ends in
a cited report. A larger cloud model plans the fields, gives one final review and writes the report. The
local model does the research.

It is general purpose. Finance and science questions are the test cases because their answers are easy to
check.

| Role | Model | Where |
|---|---|---|
| Research (main, PDF and web agents; self-review) and chat | Qwen 3.5 4B (`Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed`) | local, [MTPLX](https://github.com/youssofal/MTPLX), `turbo` profile |
| Plan the fields, final review, report | Gemini 3.5 Flash-Lite (Groq Qwen 27B as fallback) | cloud, about 3 calls per research |

Any model that speaks the OpenAI chat API with tool calls can take the research role: the harness reads the
model id from the server. Bonsai 2 27B was tried and dropped (about 53 s per tool turn against about 5 s for
the 4B).

FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## Principles

- **Harness, not workflow.** Code supplies tools, information and guardrails. The model makes the research
  choices. There is no deterministic re-ranking or filtering of what the model picked.
- **Prompts are domain-neutral.** Agents work like researchers: find the source first, then narrow down
  (source → section → note or table) instead of searching for field names.
- **Reviews judge substance:** the number, entity, period, item and quote support. Never format: sign,
  rounding or units.
- **Small context, small machine.** Everything is built for a 4B model with a 32k context window on a 24 GB Mac.

## What the harness gives the model

Everything below is code, not model work. The model sees only short tools that say what they do.

- **Web search.** Several engines merged per query via `ddgs` (`DR_SEARCH_BACKENDS`). Repeated queries are
  refused across the whole research. Each result's title, URL and snippet are shown before anything is opened.
- **Search history.** Every query, every link it returned, what was opened, and a remark on whether it helped
  ("helpful: recorded Net Debt from [2]", "not helpful: opened 3, nothing recorded").
- **PDFs with minimal effort.** PDFs are downloaded once and cached. Text comes from the PDF's own text layer;
  indra-ocr converts a page (tables as HTML) when it is read in full or has no text layer.
- **Document preparation**, once per PDF, before any agent sees it:
  - a description from its first pages, skipping cover letters to the exchange;
  - the contents page, in any common layout, and a notes index;
  - printed-to-PDF page mapping per section, including two-page spreads, checked against the page numbers
    printed on the pages;
  - a BM25 keyword index of every page.
- **Search inside PDFs.** `locate("net debt March 2026")` returns the best pages as printed page, section and
  matching passage. `read_pages(from, to)` returns whole pages by printed page number.
- **Table-aware recording.** A value can be recorded from a row label and a number however the agent names
  the row. The harness reads indra-ocr's HTML tables (colspan, serial-number columns, split headers) and checks
  the value on every page read in the call.
- **Guardrails in the tools.**
  - A value needs an exact quote from a page read in full that contains the value's digits.
  - Documents outrank web pages.
  - Steps, pages and hand-offs have budgets.
  - "Not public" is allowed only after both the PDF route and the web route were tried.
- **Loops that survive a small context.** One shared tool loop for every agent:
  - at most 3 tool calls per turn;
  - a separate planning call with thinking on and no tools, before the first turn and after a failed attempt;
  - a nudge when the model stops using tools;
  - compaction (old page texts replaced by placeholders) before the context window fills;
  - a pause instead of a failure when the Mac is short of memory or the model server is down.

## How a research runs

1. **Chat (local).** Each message is answered, sent to a quick web search (`search:`), or started as research
   (`task:`). Replies stream token by token.
2. **Plan (cloud).** Given today's date, the request becomes **fields** grouped into sub-tasks by source,
   plus **documents** worth finding (annual reports, filings, rating rationales, papers, statistics).
3. **Documents.** Each planned document is searched for. The model picks the candidates and checks their
   first pages: is it the entity itself (not a related company), and is it dated right? Confirmed PDFs are
   prepared in the background.
4. **Explore (local, one sub-task at a time).** Three loops, each with its own prompt and tools:
   - the **main agent** decides, field by field, whether to look in a document or on the web, and hands the
     work over (`explore_document`, `search_web`, `mark_not_public`, `done`);
   - the **PDF agent** gets one document and a few fields (`locate`, `list_notes`, `read_pages`,
     `record_value`, `report`);
   - the **web agent** gets the objective and the search history: search, open, record, report. PDFs it finds
     become documents for the main agent.
5. **Self-review (local, thinking on).** Accepts values, rejects them, or sends fields back with a hint for
   another loop (up to `DR_MAX_RESTARTS`).
6. **Consistency checks.** Run only when every figure of an identity was found (e.g. assets = liabilities +
   equity). Mismatches add a note.
7. **Final review and report (cloud).** One review of all values against their quotes: it can accept or
   reject, never invent "not public". Then a cited report with figures in tables and a Gaps section saying
   where missing fields were looked for.

Researches run one at a time (a second one shows "queued"). Chat keeps working meanwhile.

## Speed

Measured on an M4 Pro with 24 GB, details in [`docs/eval/2026-11-26-mtplx-speed.md`](docs/eval/2026-11-26-mtplx-speed.md):

- Decode runs at about 100 tok/s on `turbo` (74 tok/s at 25k context, against 55 on MTPLX's default
  `sustained`).
- Prefill runs at about 600 tok/s, the same as stock `mlx_lm`. MTPLX's RAM prefix cache means a follow-up
  turn re-reads only the new text.
- On `sustained`, a narrow question took 2-12 minutes and a broader report 15-20 minutes. About 70% of that
  is the local model, about 25% web search and downloads.

## Prerequisites

- macOS on Apple Silicon (tested on an M4 Pro, 24 GB)
- MTPLX (`brew install youssofal/mtplx/mtplx`, `pip install mtplx`, or the app) with
  `Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed`
- Python 3.13 with `pip install -r requirements.txt`
- Node.js (for `frontend/`)

## Setup

Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY` and `GROQ_API_KEY`. Every other setting has a
default:

| Setting | Default | Meaning |
|---|---|---|
| `DR_RESEARCH_URL`, `DR_CHAT_URL` | `http://127.0.0.1:8002/v1` | Local model server (one server serves both roles) |
| `DR_CONTEXT_TOKENS` | 28000 | Compact the agent conversation past this |
| `DR_THINK_TOKENS` | 1200 | Thinking budget of an agent's planning call |
| `DR_PDF_STEPS`, `DR_WEB_STEPS` | 20 | Tool calls per PDF or web agent call |
| `DR_MAX_DELEGATIONS` | 6 | Main-agent hand-offs per loop |
| `DR_MAX_SUBTASKS` | 8 | Sub-tasks per research |
| `DR_MAX_RESTARTS` | 2 | Extra loops after self-review |
| `DR_MAX_PAGES` | 150 | Pages read per research |
| `DR_SEARCH_BACKENDS` | `yahoo,bing,brave,duckduckgo,google,mojeek,startpage` | Search engines, merged |
| `DR_SEARCH_MAX_RESULTS` | 8 | Results per quick search |
| `DR_SEARCH_REGION` | `us-en` | `in-en` India, `uk-en`, `wt-wt` no region |
| `DR_DB` | `data/dr.sqlite` | SQLite path (older databases are upgraded on start) |
| `DR_LOG_LEVEL`, `DR_LOG_FILE` | `INFO`, `logs/dr.log` | `DEBUG` adds prompt and reply previews |

The cloud model IDs live in `backend/app/config.py` (`ROLES`, `DAILY_LIMITS`, `PROVIDER`) and must match what
each endpoint serves:

```
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://generativelanguage.googleapis.com/v1beta/openai/',api_key=os.environ['GEMINI_API_KEY']).models.list()])"
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://api.groq.com/openai/v1',api_key=os.environ['GROQ_API_KEY']).models.list()])"
```

## Run

Three terminals:

```
~/.mtplx/bin/mtplx serve --model Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed --host 127.0.0.1 --port 8002 \
  --no-auth --context-window 32768 --ssd-session-cache off --profile turbo
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765   # from the repo root
cd frontend && npm run dev                                             # then open http://localhost:5173
```

Keep `--ssd-session-cache off`: with it on, MTPLX wrote about 23 GB per hour to the SSD. The RAM prefix cache
stays on either way. Demo login: `analyst@demo` / `password`.

Logs (terminal and `logs/dr.log`, tagged `[c<conversation> t<task> s<sub-task>]`) record the plan, document
checks, every model call (prompt size, tool calls, time), every tool call with its arguments, pages read,
values recorded and agent reports. MTPLX's per-request speeds (prefill, decode, cache hits, draft acceptance)
are at `http://127.0.0.1:8002/metrics` (the last 32 requests).

## Tests

```
cd backend && python -m pytest -q
cd frontend && npx vitest run
```

## Limits and failure handling

- If the Mac runs short of memory, MTPLX refuses a request rather than swapping. The research then pauses
  ("paused: Mac memory low or MTPLX not running") and retries, as it does when the model server is down.
- Cloud fallbacks: Gemini Flash-Lite → Groq Qwen 27B. Rate limits, overload and timeouts cool a model down and
  try the next one. If every model is down, the research pauses and says so. A bad API key stops the research
  with a hint to check `.env`.
- Tasks don't survive a backend restart: running tasks are marked "interrupted". Ask again to rerun.
- Running fully local: the plan, review and report roles are plain OpenAI-compatible calls
  (`backend/app/cloud_llm.py`). Adding a local provider there is a small code change; it isn't a setting yet.
- Demo auth only (fixed login, no token checks on API routes): use it on localhost only.
- Public information only. `locate` is keyword search (BM25), so a query worded differently from the
  document can miss.
- Web search engines rate-limit after heavy testing (timeouts, "no results"). This is outside the harness.

## Roadmap checklist

**New file abilities**
- [ ] **XLSX reading:** open spreadsheets found on the web or in filings (sheets, named tables, merged
      headers) and give the agents the same `locate` / read / record tools and quote checks as PDFs.
- [ ] **XLSX writing:** export a research's fields (value, unit, period, source, page, quote, review level)
      and its tables to a workbook.
- [ ] **PDF writing:** export the cited report as a PDF with its tables, sources and Gaps section.
- [ ] Download buttons for the exports in the frontend.

**Planning and report quality**
- [ ] The planner first outputs the report's sections, then the fields each section needs (one cloud call,
      automatic). Store the sections and give the same outline to the report writer. Fields keep their
      source-based sub-tasks and get a section label.
- [ ] The report cites what verification found about a document, not the planned document name; drop
      boilerplate such as "all figures add up" when no identity was checked.
- [ ] Normalise ₹ rendered as a backtick in Indian PDFs ("` Mn").
- [ ] Self-review: stop rejecting correct values for wrong reasons (e.g. NABARD's name read as "agricultural
      holdings"), and check that the recorded item is the asked item (total borrowings ≠ bonds outstanding).

**Speed** (see the speed report)
- [ ] Cut thinking-only planning calls (28% of model time): a smaller `DR_THINK_TOKENS` or fewer planning calls.
- [ ] Measure how many no-tool replies are wasted, nudged turns, and reduce them.
- [ ] Web search: fewer engines per query, or a timeout on slow engines (15-30 s per search today).
- [ ] Measure SSD writes per minute of a research run with the SSD cache off.

**Retrieval and scope**
- [ ] Embedding search next to BM25 for `locate` (costs memory on a 24 GB Mac).
- [ ] A fully local option as a setting (plan, review and report on a local server).
- [ ] Wider tests: policy (fiscal deficit from budget documents), multi-section reports (a debt-bond
      investment report), and "market viability of a product".
- [ ] Parked: reviewers flagging second-hand sources; a LoRA fine-tune of the 4B on its own accepted traces.
