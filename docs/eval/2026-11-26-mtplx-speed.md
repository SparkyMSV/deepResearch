# MTPLX speed check: Qwen 3.5 4B on an M4 Pro (24 GB)

2026-11-26. Model `Youssofal/Qwen3.5-4B-MTPLX-Optimized-Speed` (4-bit, native MTP), MTPLX 2.13.3, MLX 0.32.2.

## Verdict

- **Serve with `--profile turbo`.** It decodes 10-35% faster than `sustained`, the profile the MTPLX app picks
  for the 4B (MTPLX turns turbo on by default only for its 27B/9B flagships). Turbo adds a compiled verify step
  (`MTPLX_COMPILED_VERIFY`) and fused 4-bit verify matmul kernels (`MTPLX_NAX_VERIFY`). Prefill is identical.
- **Prefill has no waste to recover.** About 600 tok/s, the same as stock `mlx_lm` on the same weights, and
  larger prefill chunks don't help. That is the compute cost of this model on this GPU.
- **The RAM prefix cache works.** A second turn on the same prompt starts in 0.3-0.7 s instead of 47 s. The
  harness keeps prompt prefixes stable well enough: in real runs, calls on 12-20k-token prompts took 5-6 s.
- **Keep draft depth 3.** Depth 1 is fastest for prose and depth 3 for JSON and tool calls, which is most of
  what the harness produces.
- `mtplx profile compile-audit` doesn't run on the app install: it needs research scripts that aren't shipped.

## Benchmark (scratch server, one case at a time, thinking off)

Harness-shaped requests: the PDF agent's tools, a long document page, a short tool or text reply.

| Case | `sustained` | `turbo` | Stock `mlx_lm` |
|---|---|---|---|
| Cold prompt 5.2k tokens: time to first token | 8.2 s | 8.2 s | ~665 tok/s |
| Cold prompt 13k tokens: time to first token | 22.1 s | 22.1 s | ~630 tok/s |
| Cold prompt 25.7k tokens: time to first token | 47.0 s | 47.0 s | ~550 tok/s |
| Second turn on the same prompt: time to first token | 0.3-0.7 s | 0.3-0.6 s | - |
| Decode at 13k context | 72 tok/s | 77 tok/s | 73 tok/s |
| Decode at 25k context | 55 tok/s | **74 tok/s** | 63 tok/s |
| Decode, prose (short prompt) | 77 tok/s | 86 tok/s | 81-95 tok/s |
| Decode, JSON (short prompt) | 111 tok/s | 122 tok/s | - |

Draft depth on turbo (average of 2 runs):

| Depth | Prose | JSON |
|---|---|---|
| D1 | 94 tok/s | 109 tok/s |
| D2 | 88 tok/s | 116 tok/s |
| D3 | 85 tok/s | 128 tok/s |

Each case ran once (the depth runs twice), so read differences under about 10% as noise.

## Where a research run's time goes (`sustained`, logs from 2026-11-25)

Six-task benchmark (`hallucination.csv`, runs qwen-bench1-t1..t6): 51 min of wall time.

| Part | Share |
|---|---|
| Local model | ~70% (37 min, 353 calls) |
| Web search, page opens, PDF downloads, OCR | ~25% (each search takes 15-30 s; 7 engines merged) |
| Cloud (plan, final review, report) | ~2% |

Local-model time by call type:

| Call type | Calls | Time | Avg per call |
|---|---|---|---|
| Tool calls | 243 | 16.1 min (44%) | 4.0 s |
| Thinking-only calls with an empty answer (planning calls) | 32 | 10.2 min (28%) | 19.1 s |
| Text replies with no tool call (planning answers and nudged turns) | 78 | 10.2 min (28%) | 7.9 s |

The single debt-bond report (20 min) has the same shape. Compaction ran once in the six tasks.

## Live run on turbo (NABKISAN Finance debt bonds, stopped by the user after 8.5 min)

- Decode, from the server's last 32 requests: median 99 tok/s, 10th-90th percentile 79-119 tok/s, 107 tok/s
  on prompts of 12k tokens and up. Draft acceptance was 68%.
- Of the model time, 89 s went to decode and 71 s to reading new prompt text (41k uncached tokens at about
  575 tok/s). On cached prompts, the new text at the end of each turn (pages just read) is now nearly half
  the model time.
- Result when stopped: 3 of 6 fields found. Ownership (NABARD 89.12%) was rejected by self-review for a wrong
  reason, then found again. "Total outstanding debt bonds" was recorded as total borrowings of ₹6,137.15
  crore, the wrong item. The rating value was mislabelled (the CP rating A1+ instead of the bonds' AAA). The
  plan had only 6 fields: the short-checklist problem.

## What to do next for speed (not done)

1. Cut the thinking-only planning calls: a smaller `DR_THINK_TOKENS`, or fewer planning calls per loop (28% of
   model time).
2. Find out how many no-tool text replies are wasted, nudged turns. The log doesn't say yet.
3. Web search: fewer engines per query, or a timeout on slow engines.
4. Fewer pages read in full per turn, since uncached new text costs about 1.7 ms per token.
