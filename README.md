# Deep Research Harness

A harness that lets a small local model, Qwen 3.5 4B running on a MacBook with MLX, explore the web and
PDFs to answer research questions. Everything else in this repo exists to make that exploration work:

- **Larger cloud models** (Gemini Flash-Lite, Groq Qwen 27B) set targets, review progress and write the
  final report. They plan and supervise; they are not meant to do the exploring.
- **The harness** gives Qwen small, well-scoped decisions with compact context, and guards the results:
  no repeat searches or pages, free screening on search-result URLs and snippets, verbatim quotes for
  every extracted passage, budgets, and fallbacks when cloud models are down.
- **The chat UI** shows what Qwen is doing, round by round.

FastAPI backend in `backend/`, React + Vite frontend in `frontend/`.

## How it works

1. **Chat.** Qwen reads each message and either answers, runs a quick web search (`search:`), or starts a
   research task (`task:`). Replies stream token by token.
2. **Plan (larger model).** Given today's date, it turns the request into a checklist of **fields**: specific
   figures or short facts, each for a named entity and period (e.g. "Gross NPA %, Navi Finserv, FY2025-26"),
   grouped into sub-tasks, with starting searches, hints on where each value usually appears, and the
   **documents** worth finding (annual reports, filings, rating rationales).
3. **Documents (once per research).** Each planned document is searched for. Candidates whose URL, title
   and DuckDuckGo snippet don't name the company are dropped for free; for the best three, indra-ocr turns
   page 1 into text and Qwen checks it is the right document. Verified PDFs are shared with every sub-task.
4. **Explore (Qwen).** Each sub-task works on one open field at a time. Every step is one small Qwen
   decision with a compact view (the field, a hint, what was tried, a short menu):
   - **Read a document**: Qwen sees its contents list, picks a section or page, and reads page by page
     (up to 6 per visit). Pages are OCR'd on demand and cached for the whole research. Printed page
     numbers are mapped to real PDF pages.
   - **Search the web**: Qwen sees each result's URL, title and snippet and opens up to three.
   - **Not public**: Qwen can say a value isn't publicly available.
   On every page Qwen reports values for any open field with an exact quote; values whose quote isn't on
   the page are dropped. Document values outrank web values. After 3 steps without progress, focus
   moves to the next field.
5. **Check in (larger model).** Every 5 steps, when stuck, or when everything is filled, the larger model
   reviews the fields table: accept, reject, not public, or retry with a hint or new search; it can add
   fields or ask for another document.
6. **Report (larger model).** Written from the fields table, with figures in tables, sources with pages,
   and a Gaps section. Qwen adds a short summary in chat, and follow-up questions can use the fields.

Guardrails in code: no repeat or near-repeat searches (80% shared words), no re-reading pages or URLs,
verbatim quotes, page budget (`DR_MAX_PAGES`), step and restart limits.

## Roadmap

- **Optimise Qwen on the MacBook.** Make the local model faster and lighter: prompt caching, batching,
  quantisation and context-length choices, and measuring time per call. The harness makes many small
  Qwen calls, so this matters.
- **Document/OCR retrieval.** Revisit how information is retrieved from documents, and the OCR step
  with it.

## Prerequisites
- macOS on Apple Silicon
- Python 3.13 with `pip install -r requirements.txt` (fastapi, uvicorn, sse-starlette, mlx-lm, ddgs,
  trafilatura, requests, indra-ocr, pypdfium2, openai, python-dotenv, tzdata, pytest, and pymupdf for the
  PDF tests)
- Node.js (for `frontend/`)
- MLX model `mlx-community/Qwen3.5-4B-4bit` in the Hugging Face cache
  (`huggingface-cli download mlx-community/Qwen3.5-4B-4bit`)

## Setup
Copy `.env.example` to `.env` and fill in:
- `GEMINI_API_KEY`, `GROQ_API_KEY`
- Optional limits (defaults): `DR_MAX_SUBTASKS=8`, `DR_ROUNDS_PER_LOOP=20` (Qwen steps per loop),
  `DR_MAX_RESTARTS=2` (check-ins that may reopen work), `DR_MAX_PAGES=150` (pages Qwen may read per research),
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
```
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765   # from the repo root
cd frontend && npm run dev                                             # then open http://localhost:5173
```
Demo login: `analyst@demo` / `password`. The first message loads the local model, so it is slow.

## Limits and failure handling
- A research with 7 sub-tasks can take 30–60 minutes: every page goes through the 4B model on the Mac.
  It makes only about 20–35 Gemini/Groq calls (plan, check-ins, report).
- A sub-task runs up to `DR_ROUNDS_PER_LOOP` steps per loop; a check-in that reopens work starts a new
  loop, at most `DR_MAX_RESTARTS` times; then it ends `partial`, and its unreviewed values are marked so.
- Cloud fallbacks: Gemini Flash-Lite → Groq Qwen 27B (check-ins: Groq first, then Gemini). Rate limits (429), overload
  (5xx) and timeouts cool a model down for 60 s and try the next; the chat says when it switches from
  Gemini to Groq. If every model is unavailable the research pauses, says so, and retries (every minute,
  or every 15 minutes once daily quotas are used up; they reset at midnight Pacific). A bad API key stops
  the research with a hint to check `.env`.
- Tasks don't survive a backend restart: running tasks are marked "interrupted". Ask again to rerun.
- Chat keeps working while a task runs (Qwen answers chat ahead of research work). Without research
  evidence, direct answers come from Qwen's general knowledge and cite no sources.

## Known limits
- Shortcut: demo auth only (fixed login, no token checks on API routes); localhost use only.
- Public information only.
