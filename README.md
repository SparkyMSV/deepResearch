# Deep Research Harness

A harness that gives a **local model** what it needs to finish a research task on its own machine: it can search
the web, open pages, download PDFs, turn them into text with almost no effort, search inside them, and run
separate tool loops for separate jobs. The model only decides what to do next; the harness does the
heavy lifting and enforces the rules.

The goal is **research completion**: a checklist of **fields** (a figure or short fact for an entity and period,
e.g. "Net Debt, Mindspace REIT, FY2025-26") is filled with values, each with a source and an exact quote, and
written up as a cited report. A larger model decides the fields and writes the report; the local model does
the research.

In this setup:

| Role | Model | Where |
|---|---|---|
| Research (main, PDF and web agents; self-review) | Ternary Bonsai 2 27B | local, [MTPLX](https://github.com/youssofal/MTPLX) |
| Chat | Qwen 3.5 4B | local, MTPLX |
| Plan the fields, final review, report | Gemini 3.5 Flash-Lite (Groq Qwen 27B as fallback) | cloud |

Any model that speaks the OpenAI chat API with tool calls can take the research role. The planning, review and
report roles are plain OpenAI-compatible calls too, so the whole thing can run locally (see
[Running fully local](#running-fully-local)).

FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## What the harness gives the model

Everything below is code, not model work. The model sees only short tools that say what they do.

- **Web search.** Several engines merged per query via `ddgs` (`DR_SEARCH_BACKENDS`), with a fallback;
  repeated queries are refused across the whole research; each result's title, URL and snippet are shown so the
  model can choose before opening anything.
- **Search history.** Every query, every link it returned, what was opened and a remark on whether it helped
  ("helpful — recorded Net Debt from [2]", "not helpful — opened 3, nothing recorded").
- **PDFs with minimal effort.** PDFs are downloaded once and cached; text comes from the PDF's own text layer,
  and indra-ocr converts a page to text (tables as HTML) when it's read in
  full or has no text layer.
- **Document preparation** (once per PDF, before any agent sees it):
  - a description from its first pages, past cover letters to the exchange;
  - the contents page, in any common layout ("Title ..... 12", "Title 12", "12 Title", multi-column);
  - printed-to-PDF page mapping per section, including reports laid out as two-page spreads, checked against
    the page numbers printed on the pages;
  - a BM25 keyword index of every page.
- **Search inside PDFs.** `locate("net debt March 2026")` returns the best pages as printed page, section and
  matching passage; `read_pages(from, to)` returns whole pages by printed page number. Offsets, extraction and
  OCR stay hidden.
- **Guardrails in the tools.** Values are only recorded with an exact quote from a page read in full that
  contains the value's digits; documents outrank web pages; budgets on steps, pages and hand-offs; "not
  public" only after both the PDF and the web route looked.
- **Loops that survive a small context.** One shared tool loop for every agent: at most 3 tool calls per turn, a
  nudge when the model stops using tools, compaction (old page texts replaced by placeholders) before the
  context window fills, and a pause instead of a failure when the Mac is short of memory or the model server
  is down.

## How a research runs

1. **Chat (local 4B).** Each message is answered, sent to a quick web search (`search:`), or started as research
   (`task:`). Replies stream token by token.
2. **Plan (larger model).** Given today's date, the request becomes **fields** grouped into sub-tasks, plus
   **documents** worth finding (annual reports, filings, rating rationales).
3. **Documents.** Each planned document is searched for; candidates whose URL, title and snippet don't name
   the company are dropped; the local model checks the first pages of the best three. Confirmed PDFs are
   prepared (above).
4. **Explore (local, one sub-task at a time).** Three loops, each with its own prompt and tools:
   - the **main agent** decides, per field, whether to look in a document or on the web, and hands the work
     over (`explore_document`, `search_web`, `mark_not_public`, `done`);
   - the **PDF agent** gets one document and a few fields: locate anchors, read those pages in full, record
     values with quotes, report;
   - the **web agent** gets the objective and the search history: search, open, record, report. PDFs it finds
     become documents for the main agent.
5. **Self-review (local, thinking on).** Accepts values, rejects them, or sends fields back with a hint for
   another loop (up to `DR_MAX_RESTARTS`).
6. **Consistency checks.** Only when every figure of an identity was found (e.g. assets = liabilities +
   equity); mismatches add a note.
7. **Final review and report (larger model).** One review of all values against their quotes (it can accept or
   reject, never invent "not public"), then a cited report with figures in tables and a Gaps section saying
   where missing fields were looked for.

Researches run one at a time (a second one shows "queued"); chat keeps working meanwhile.

## Running fully local

The planning, review and report roles are listed in `backend/app/config.py` (`ROLES`, `PROVIDER`,
`DAILY_LIMITS`) and called through `backend/app/cloud_llm.py`, which knows two OpenAI-compatible providers
(Gemini, Groq). To run without the cloud, add a provider pointing at a local OpenAI-compatible server (MTPLX,
llama.cpp, Ollama, LM Studio) in `cloud_llm.py` and list that model in `ROLES`. This is a small code change; it
isn't a setting yet. Expect the plan to need a capable model: it decides what "done" means.

## Roadmap

- **Optimise the local models on the MacBook**: MTP depth tuning (`mtplx tune`), session cache settings,
  context-window choices.
- **Retrieval inside documents**: embedding search next to BM25 for `locate` (costs memory on a 24 GB Mac).
- **Fully local option** as a setting.
- **Research-minded prompts**: agents should first work out what the task means and where such figures are
  found (which source, then which part: section → note → table), narrowing step by step, instead of searching
  field names. For "market viability of a product", search the higher meaning (market size, demand,
  competitors, pricing), not the product name.

## Prerequisites
- macOS on Apple Silicon (tested on an M4 Pro, 24 GB)
- MTPLX (`brew install youssofal/mtplx/mtplx`, `pip install mtplx`, or the app) with the models
  `Youssofal/Ternary-Bonsai-2-27B-MTPLX-Optimized-Speed` and `Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed`
- Python 3.13 with `pip install -r requirements.txt`
- Node.js (for `frontend/`)

## Setup
Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY`, `GROQ_API_KEY`
- Local models: `DR_RESEARCH_URL` (default `http://127.0.0.1:8001/v1`), `DR_CHAT_URL` (default
  `http://127.0.0.1:8002/v1`)
- Optional limits (defaults): `DR_MAX_SUBTASKS=8`, `DR_MAX_DELEGATIONS=6` (main-agent hand-offs per loop),
  `DR_PDF_STEPS=12`, `DR_WEB_STEPS=12` (tool calls per PDF/web agent call),
  `DR_MAX_RESTARTS=2` (extra loops after self-review), `DR_MAX_PAGES=150` (pages read per research),
  `DR_CONTEXT_TOKENS=12000` (compact the agent conversation past this),
  `DR_SEARCH_BACKENDS=yahoo,bing,brave,duckduckgo,google,mojeek,startpage` (search engines, merged),
  `DR_SEARCH_MAX_RESULTS=8` (results per quick search), `DR_SEARCH_REGION=us-en` (`in-en` India, `uk-en`,
  `wt-wt` no region)
- Optional: `DR_DB` (SQLite path, default `data/dr.sqlite`; older databases are upgraded on start)
- Logs: plan, document checks and preparation, each model call (prompt size, tool calls, time), pages read,
  values recorded and agent reports go to the terminal and `logs/dr.log` (rotating, 5 × 5 MB), each line tagged
  `[c<conversation> t<task> s<sub-task>]`. `DR_LOG_LEVEL=DEBUG` adds prompt and reply previews; `DR_LOG_FILE`
  changes the path.

### Verify model IDs (once, after adding keys)
The IDs in `backend/app/config.py` (`ROLES`, `DAILY_LIMITS`, `PROVIDER`) must match what each endpoint
serves:
```
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://generativelanguage.googleapis.com/v1beta/openai/',api_key=os.environ['GEMINI_API_KEY']).models.list()])"
python -c "import os;from dotenv import load_dotenv;from openai import OpenAI;load_dotenv();print([m.id for m in OpenAI(base_url='https://api.groq.com/openai/v1',api_key=os.environ['GROQ_API_KEY']).models.list()])"
```
Look for `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` and `qwen/qwen3.8-27b`.

## Run
Four terminals (start the two model servers first):
```
~/.mtplx/bin/mtplx serve --model Youssofal/Ternary-Bonsai-2-27B-MTPLX-Optimized-Speed --host 127.0.0.1 --port 8001 --no-auth --context-window 16384
~/.mtplx/bin/mtplx serve --model Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed --host 127.0.0.1 --port 8002 --no-auth --context-window 16384
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765   # from the repo root
cd frontend && npm run dev                                             # then open http://localhost:5173
```
Demo login: `analyst@demo` / `password`. Both servers together use about 10–13 GB under load.

## Limits and failure handling
- A research with two sub-tasks takes roughly 20–40 minutes on a 24 GB Mac; most of it is the local model
  reading pages. About 3 cloud calls per research (plan, final review, report).
- If the Mac runs short of memory, MTPLX refuses a request rather than swapping; research then pauses
  ("paused — Mac memory low or MTPLX not running") and retries, as it does when a model server is down.
  If the chat server is down, chat says so.
- Cloud fallbacks: Gemini Flash-Lite → Groq Qwen 27B; rate limits, overload and timeouts cool a model down
  and try the next; if every model is down the research pauses and says so. A bad API key stops the research
  with a hint to check `.env`.
- Tasks don't survive a backend restart: running tasks are marked "interrupted". Ask again to rerun.

## Known limits
- Shortcut: demo auth only (fixed login, no token checks on API routes); localhost use only.
- Public information only.
- `locate` is keyword search (BM25): a query worded differently from the document can miss.
