# Deep Research Harness

A harness that lets a local model explore the web and PDFs to answer research questions on a MacBook.
Research runs on **Ternary Bonsai 2 27B** (Qwen3.8-27B in ternary weights) and chat on **Qwen 3.5 4B**,
both served by [MTPLX](https://github.com/youssofal/MTPLX) with multi-token prediction. Cloud models
(Gemini Flash-Lite, Groq Qwen 27B) only plan, give one final review, and write the report.

FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## How it works

1. **Chat (4B).** Each message is answered, sent to a quick web search (`search:`), or started as research
   (`task:`). Replies stream token by token.
2. **Plan (cloud).** Given today's date, the request becomes a checklist of **fields** (a figure or short
   fact for an entity and period, e.g. "Gross NPA %, Navi Finserv, FY2025-26") grouped into sub-tasks, plus
   **documents** worth finding (annual reports, filings, rating rationales).
3. **Documents.** Each planned document is searched for; candidates whose URL, title and snippet don't name
   the company are dropped for free; Bonsai checks page 1 of the best three.
4. **Prepare documents (no model).** Each confirmed PDF is described from its first pages (past any
   cover letter to the exchange), its contents page is read, the page offset is worked out per section
   (printed vs PDF page numbers), and a keyword index of every page is built.
5. **Explore (Bonsai, one sub-task at a time).** A **main agent** decides, per field, where to look and hands
   the work over:
   - the **PDF agent** gets one document and a few fields; it uses `locate` to find anchor pages, reads whole
     pages with `read_pages` (printed page numbers; offsets and OCR are handled inside the tool) and records
     values with exact quotes;
   - the **web agent** gets the objective and the research's search history (every query, every link it
     returned, and whether it helped); it searches, opens results and records values. PDFs it finds become
     documents for the main agent.
   Only the main agent may mark a field "not public", and only after both routes reported it missing.
   Guardrails stay in the tools: exact quotes, no repeat searches, documents outrank web pages, budgets.
6. **Self-review (Bonsai, thinking on).** Accepts, rejects or sends fields back with a hint; up to
   `DR_MAX_RESTARTS` more loops.
7. **Consistency checks.** Only when every figure of an identity was found (e.g. assets = liabilities +
   equity): mismatches add a note, nothing is rejected.
8. **Final review and report (cloud).** One review of all values (confirmed / checked locally /
   unreviewed), then a cited report with figures in tables and a Gaps section.

Researches run one at a time (a second one shows "queued"); chat keeps working meanwhile.

## Roadmap

- **Optimise the local models on the MacBook**: MTP depth tuning (`mtplx tune`), session cache settings,
  context-window choices.
- **Document/OCR retrieval**: revisit how information is retrieved from documents, and the OCR step.

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
  `DR_SEARCH_MAX_RESULTS=8` (DuckDuckGo results per search; every one is fetched and read, so higher =
  slower rounds), `DR_SEARCH_REGION=us-en` (`in-en` India, `uk-en`, `wt-wt` no region)
- Optional: `DR_DB` (SQLite path, default `data/dr.sqlite`)
- Logs: every step (plan fields, document checks, each Qwen choice and reply, pages read, values filled, check-in actions, cloud calls) goes to the
  terminal and `logs/dr.log` (rotating, 5 × 5 MB), each line tagged `[c<conversation> t<task> s<sub-task>]`.
  `DR_LOG_LEVEL=DEBUG` adds prompt and reply previews; `DR_LOG_FILE` changes the path.

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
- About 15–25 s per page Bonsai reads; a 5-sub-task research reading ~40 pages takes roughly 15–30 minutes,
  with about 3 cloud calls (plan, final review, report).
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
