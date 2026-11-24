# Research harness for a capable local model (Bonsai 2 27B on MTPLX)

Status: draft for approval. Builds on `2026-11-24-field-checklist-redesign-design.md` (field checklist,
documents, verbatim quotes) and replaces its step-menu exploration with a tool-using agent loop.

## Intent

The project stays a **harness for the local model**: the local model explores the web and PDFs and
pulls out the figures; the cloud models plan and write the report. What changes is the local model.
Research moves from Qwen 3.5 4B to **Ternary Bonsai 2 27B** (Qwen3.8-27B in ternary weights), served by
**MTPLX** with native multi-token prediction. Chat stays on Qwen 3.5 4B, also on MTPLX.

The 4B harness was built around a small model's weaknesses: one tiny decision per call, menus with
examples to copy, code deciding where to go, a cloud reviewer every few steps. A 27B with good
instruction following (IFEval 91.3) and tool calling (BFCL 74.9) can drive its own exploration through
tools, so the harness becomes: good tools, guardrails inside the tools, a cache-friendly context, and
budgets.

## Measured on this Mac (M4 Pro, 24 GB) — the numbers the design rests on

- Bonsai, thinking off: a fresh 1,081-token page read took 13.2 s; 2,295 tokens 24.3 s. Prompt
  reading (~90 tokens/s) dominates; writing the short answer is fast.
- Repeating an identical prompt: 1.9–2.5 s. A growing conversation (10 pages, up to 14.8K tokens) cost
  11–27 s per turn — only the new page is read, earlier turns are reused.
- Quality: correct entity/period judgement (took CRISIL's AAA/Stable; skipped "segment liabilities" that
  the 4B mistook for total debt); well-formed tool calls (`record_value` then `read_page` in one turn).
- Thinking: per request with `chat_template_kwargs: {"enable_thinking": false}`; with `true` the server
  uses its default effort (medium).
- Tool calls come back in the structured OpenAI `tool_calls` field, and `usage.prompt_tokens` is filled
  (seen in the tests), so the client uses the SDK's tool loop, no text parsing.
- Memory with both servers under load (OCR + Bonsai conversation + 4B chat every 5 s): Bonsai 7.8 → 10.2
  GB, 4B 2.4 GB, backend 1.0 GB, Mac free memory down to 8%, no failures. MTPLX refuses a request it
  can't fit (HTTP 507 `insufficient_memory`, "retry can succeed") instead of swapping.
- 4B chat latency: ~2 s alone, 1.3–9.2 s (typ. ~6 s) while Bonsai works (shared GPU).

## Goals

1. Each sub-task is one **agent loop**: Bonsai calls tools to search, open pages, find and read PDF
   pages, and record field values, until its fields are filled or it decides they are not public.
2. **Guardrails live inside the tools**: verbatim-quote check, no repeat searches or pages, document
   values outrank web values, accepted values are final, page and step budgets.
3. **Context is cache-friendly and bounded**: a fixed opening (rules, date note, fields, documents) is
   reused across turns; the conversation only grows at the end; it is compacted before ~12K tokens.
4. **Cloud roles shrink**: plan (unchanged), one final review of all values, report. Routine cloud
   check-ins go away; Bonsai reviews its own fields at the end of each loop.
5. **Both local models run on MTPLX** and are reached over its OpenAI-compatible API; the backend no
   longer loads MLX itself.
6. Memory refusals (507) and a stopped MTPLX server pause and retry, with a visible status, rather than
   failing the research.

## Non-goals

- Changing the plan format, the documents/fields tables, the UI layout, chat routing, or quick search.
- Fully local planning/reporting (Bonsai's 16K context is too small for multi-source reports).
- Images/vision (the Bonsai pack has a vision tower; not used yet).
- Running research sub-tasks in parallel.

## Anti-goals

- A value reaching the report as accepted without a quote found verbatim on the page it cites.
- The loop running into the 16K context limit or the Mac's memory limit and failing.
- Chat becoming unusable while research runs.
- Thinking left on by default (it multiplies step time).

## Approaches considered

| | Approach | Verdict |
|---|---|---|
| A | **One tool-using agent loop per sub-task; cached prompt opening; Bonsai self-review; cloud plan + final review + report** | **Chosen** |
| B | Keep the step-menu harness, swap the model | Rejected: every small decision rereads a big prompt; wastes the model |
| C | Fully local (Bonsai also plans and reports) | Rejected: 16K context too small for the report |

## Design

### 1. Local model servers and client

- Two MTPLX servers, started by the user before the backend (README documents the commands):
  - research: `mtplx serve --model Youssofal/Ternary-Bonsai-2-27B-MTPLX-Optimized-Speed --port 8001
    --no-auth --context-window 16384`
  - chat: `mtplx serve --model Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed --port 8002 --no-auth
    --context-window 16384`
- Settings: `DR_RESEARCH_URL` (default `http://127.0.0.1:8001/v1`), `DR_CHAT_URL` (default
  `http://127.0.0.1:8002/v1`); model ids read from each server's `/v1/models` at startup.
- `local_llm.py` becomes an OpenAI-client wrapper: `async local(role: "chat"|"research", messages,
  tools=None, max_tokens=..., think=False, on_token=None)`. Thinking off unless `think=True`; streaming
  for chat via `on_token`. MLX loading and the in-process priority queue go away (each model has its
  own server; the research server runs one request at a time).
- Failure handling: 507 `insufficient_memory` → wait (start 10 s, double to 2 min) and retry, status
  `paused_memory` on the sub-task, one chat message per pause ("The Mac is short of memory; research is
  paused until it frees up"). Connection refused → same pause with "Start MTPLX (see README)".
  Startup logs a warning if either server is down.

### 2. Research flow (per research)

1. **Plan** (cloud, unchanged `plan.py`): documents + sub-tasks with fields, hints, searches.
2. **Documents** (unchanged `find_document` search + free screen); the page-1 check is asked of Bonsai.
3. **Sub-tasks run one at a time** (cache stays warm; memory stays bounded). Each runs §3.
   *(Builder call)* **Researches also queue globally**: one research at a time across all conversations
   (interleaving would lose the cached opening and the time estimate). A waiting research shows status
   `queued` ("waiting for the current research to finish"); chat is unaffected.
4. **Final review** (cloud, one call over all sub-tasks' fields): accept / reject / unavailable per
   field, with a reason. Rejected values are cleared and shown as gaps (no reopening at this stage).
   *(Builder call)* Review levels in `fields.accepted`: 0 = unreviewed, 1 = accepted by Bonsai's
   self-review, 2 = confirmed by the cloud final review. "Accepted is final" means: during exploration,
   `record_value` never overwrites a level-1 value; only the cloud final review may clear it. If the
   final review fails (bad JSON twice, or all providers down beyond the pause), values keep their level
   and the report labels them "confirmed", "checked locally" or "unreviewed".
5. **Report** (cloud, unchanged prompt and inputs).

### 3. The agent loop (one sub-task)

**Opening (fixed for the whole loop, cached):** system rules (how to work, quote exactly, one field
entity/period at a time, prefer documents for figures, stop when done), the date note, the sub-task's
fields numbered `[1]…` with entity, period, hint, and the verified documents (key, about, covers,
pages). Tool schemas are part of the opening.

**Turns:** Bonsai replies with tool calls; the harness runs them and appends the results; repeat.
Up to 3 tool calls per turn are run; more are refused with a note.

**Tools** (results are short and plain text; every result ends with a one-line field status
`filled 2/5: [1] ✓ [2] ✓ [3] – [4] – [5] not public?`):

| Tool | Does | Guardrails |
|---|---|---|
| `search(query)` | DDG; numbered results (title, URL, snippet), entity-named first | refuses repeats/≥80% overlap; skips URLs already opened |
| `open_result(n)` | fetch result n; returns ≤6,000 chars around field keywords; a PDF result is checked and becomes a new document instead | counts toward page budget |
| `pdf_contents(doc)` | contents entries, or "no contents page" | — |
| `pdf_find(doc, terms)` | pages mentioning each term (text layer), ranked | — |
| `pdf_page(doc, page)` | page text (indra-ocr, cached), ≤6,000 chars | page budget; refuses pages already read in this sub-task |
| `record_value(field, value, unit, period, quote)` | records a value from the **last page returned** | verbatim quote (≥12 chars, `…` elision, markdown-safe normalisation), value digits in quote, document beats web, accepted is final; returns the reason when rejected |
| `mark_not_public(field, reason)` | field → unavailable (pending review) | — |
| `done(note)` | ends the loop | — |

**Context bound:** before a turn would push the conversation past **12,000 tokens** (counted from the
server's `usage.prompt_tokens`), the harness compacts: it starts a fresh conversation with the same
opening plus a notes message — the field status table, documents/pages read, searches tried, and
Bonsai's last `done`/notes text. This costs one fresh read (~2–3K tokens) and keeps Bonsai's memory
near the measured 10 GB.

**Ending a loop:** `done`, every field found or marked not public (a web-sourced value counts; the
self-review can send Bonsai back for a document with `retry`), or the step budget (`DR_MAX_STEPS`, default 30 tool
calls). Then **self-review**: one Bonsai call (thinking on, medium) shown the fields table with quotes,
returning per field accept / reject / retry(hint). Rejects and retries with a hint start another loop
(fresh conversation, hints in the opening) up to `DR_MAX_RESTARTS` (default 2) times; otherwise the
sub-task ends `done` (all fields accepted or not public) or `partial`.
*(Builder calls)* `reject` clears the value and reopens the field (with its hint, if any). Once restarts
are used up, rejected values stay cleared (gaps) and other unaccepted values stay unreviewed. Across
loops of one sub-task, searches tried, URLs opened and PDF pages read are kept and still refused; the
new opening lists every field with its status, and closed fields are marked "done, don't work on".

### 4. Chat

Unchanged behaviour (router, quick search, answers, report summary), served by the 4B on `DR_CHAT_URL`
with streaming. The research loop never uses the chat server.

### 5. Limits and settings

- Kept: `DR_MAX_SUBTASKS`, `DR_MAX_RESTARTS`, `DR_MAX_PAGES`, search settings, log settings.
- New: `DR_RESEARCH_URL`, `DR_CHAT_URL`, `DR_MAX_STEPS` (30), `DR_CONTEXT_TOKENS` (12000).
- Retired: `DR_ROUNDS_PER_LOOP`, `CHECKIN_EVERY` (no routine check-ins); old `.env` values are
  ignored with a log line.
- Expected time: ~15–25 s per page read; a 5-sub-task research reading ~40 pages ≈ 15–30 min; cloud
  calls ≈ 1 plan + 1 final review + 1 report (+ fallbacks).

### 6. UI

No layout change. Rounds become tool calls with readable labels (`search "…"`, `D1 p.172`,
`recorded Total Revenue = 2,110,127`, `rejected: quote not on page`), so the checklist shows what
Bonsai did. New sub-task status `paused_memory` shown as "paused — Mac memory low, retrying".

### 7. What stays

Plan, documents and fields tables, document finding and free screening, PDF store (OCR cache, text
layer search, contents), verbatim-quote rules, value precedence, the date note, Gemini → Groq fallback
and pauses, SSE events, logs (each tool call and result logged, clipped), delete, interrupted-on-restart.

### 8. What goes

The step menu (`explore.choose`), the navigator (`docs.visit`/`_pick`), Qwen page prompts
(`fields.read_page` as a prompt; its checks move into `record_value`), routine cloud check-ins
(`checkin.review` is replaced by Bonsai self-review and one cloud final review), in-process MLX.

## Future work (unchanged, still flagged)

- Optimise the local models on the MacBook (MTP depth tuning with `mtplx tune`, session cache
  settings, context-window choices).
- Document/OCR retrieval redesign.

## Left to the builder

- Exact prompts and tool descriptions (short; examples must name real values, never placeholders the
  model could copy).
- Keyword window and result formatting details within the limits above.
- Test layout, using fakes for the OpenAI-compatible local client.
