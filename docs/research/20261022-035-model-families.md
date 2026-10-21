# 20261022-035 – Model families as coder candidates (Paket B)

## Question
Which current model families run on `.224` (GTX 1080 8 GB, 6 vCPU AVX2, 23 GB RAM, llama.cpp b11696) fast and
reliably enough with Hermclaw's coder contract to compete with `coder-main` (qwen3.6:35b) in the tournament (D-018)?
Which gaps would Hermclaw have to close for them?

## Why needed
- `coder-main` is the only coder candidate left from the rc62 tournament (R-033). Since D-021 and D-022 the
  numbers of rc62 are no longer comparable, so newer families are screened before a new tournament.
- Every candidate must meet the coder contract: JSON schema answers via `response_format` (no native tool calls),
  thinking switchable or bounded, no markup in `content`, prompt cache across turns (D-021), no output loops.

## Method
Stop window per R-034 (transient unit, safety timer, health check afterwards); no inventory entry, no request to
the production router on 18080. Scripts on `.224` under `/var/tmp/hc-screen/` (raw data there as well):
`screen-candidate.sh` (bench, test server on 18090, `screen.py`), `block-<candidate>.sh`; evaluation on `.225`
with `screen_check.py` (jsonschema against `CoderAction.schema.json`, markup regex).

Steps, cheap to expensive; the first exclusion ends a candidate:
1. Speed: `llama-bench -fitt 1024 -fitc 32768 -fa 1 -t 6 -r 3 -ub 1024 -p 4096 -n 128 -d 8192` (as `ollama`);
   decode below 10 tok/s excludes.
2. Test server `--port 18090 -c 32768 -fa on -t 6 -np 1 --jinja --fit on -ub 1024`.
3. Schema: 20 requests with the gateway body (`response_format` json_schema strict), `temperature 0.2`,
   `max_tokens 8192`, the thinking setting of the planned profile. Errors: `finish_reason: length`, empty
   `content`, invalid against the schema. Zero allowed.
4. Thinking switch and markup: the profile setting against thinking on; markup left in `content`.
5. Cache: follow-up turn in the D-021 layout (system, stable user message ~10k tokens, new last message), `cache_n`.
6. Loops: two requests with up to 8k output without schema; repeated lines and how the answer stops.

## Results

| Candidate | File | Decode tg128@d8192 | Prefill pp4096@d8192 | VRAM (server) | Schema 20× | Thinking / markup | Cache follow-up | Loops 8k | Passed |
|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 26B-A4B | Ollama blob `gemma4:26b` (Q4_K_M, 17.1 GB) | 21.1 tok/s | 387 tok/s | 6.8 GB | 0 errors, 2.5–5.9 s each, reasoning 0 chars | off: 0 chars; on: 2,502 chars in `reasoning_content`; no markup | 11,056 of 11,103 tokens cached, 47 new | stop at 5.4k and 10.6k chars, 18/134 and 10/212 repeated lines (code), decode 20.3–22.1 tok/s | **yes** |
| gpt-oss-20b | ggml-org `gpt-oss-20b-MXFP4.gguf` (12.1 GB, 2026-07-16, sha256 = LFS oid) | 25.1 tok/s | 623 tok/s | 7.1 GB | 0 errors, 2.8–19.6 s each, reasoning up to 1,444 chars (low) | low: 19 chars, high: 1,880 chars; no markup | 9,499 of 9,535 cached, 36 new | stop at 10.7k and 14.3k chars, 63/376 and 47/337 repeated lines, decode 24.2–24.7 tok/s | **yes** |
| GLM-4.7-Flash | `GLM-4.7-Flash-Q4_K_M.gguf` from R-033 on `.224` (18.3 GB) | 14.4 tok/s | 86 tok/s | 7.1 GB | 0 errors, 2.8–5.5 s each, reasoning 0 chars | off: 0 chars; on: 3,447 chars; no markup | 9,340 of 9,375 cached, 35 new | stop at 8.8k and 7.4k chars, 38/271 and 39/187 repeated lines, decode 19.1–20.3 tok/s | **yes** (prefill slow) |

### Gemma 4 26B-A4B (part 1a, 2026-10-22)
- Licence Apache-2.0; file from the Ollama store (manufacturer release via Ollama), no download.
- The load log shows no `outdated gemma4 chat template` note, so the official template was not tested separately.
- Cold 11k-token prompt: prefill 429 tok/s; follow-up turn 47 new tokens. The D-021 layout works.
- Tools chosen in the 20 schema answers: `read_file`, `write_file`, `replace_text`, `run_test`, `find_text`,
  `complete_step`, all valid against the schema.
- Decode is about a third below `coder-main` (R-034: 26.7 tok/s at d24576 with ncmoe 30/ub1024; not the same depth,
  so only a rough comparison).

### gpt-oss-20b (part 1b, 2026-10-22)
- Licence Apache-2.0; official MXFP4 file from `ggml-org/gpt-oss-20b-GGUF`, older than two weeks, sha256 equal to the
  LFS oid `27cd6c43…`; stored as `/var/lib/hcbench/models/gpt-oss-20b-mxfp4.gguf`.
- Thinking cannot be switched off; with `reasoning_effort: low` (in `chat_template_kwargs`) the schema answers carry
  0–1,444 characters of `reasoning_content` and never reach `max_tokens`. The b11696 parser separates the Harmony
  channels: no `<|channel|>` markup in `content`.
- Fastest candidate: cold 9.5k-token prompt at 688 tok/s, decode 25 tok/s. Schema answers take longer than Gemma's
  (reasoning first): median about 6 s, two outliers at 18–20 s.
- The 20 answers use more `write_file`/`read_file` and fewer `complete_step` than the others; whether that matters
  shows only in the tournament.

### GLM-4.7-Flash (part 1c, 2026-10-22)
- Licence MIT. GLM-5.3-Flash is 110–181 GB and does not fit; the GLM-4.7-Flash file from R-033 is still on `.224`.
  Its header has `deepseek2.expert_gating_func = 2` (sigmoid in llama.cpp's enum); the load log of b11696 does not
  print the value, it was read from the GGUF header.
- Passes every step, but prefill is the weak spot: 86 tok/s at depth 8192 in the bench, 151 tok/s for the cold 9.4k
  prompt (about 62 s), against 429–688 tok/s for the other two. Coder turns are prefill-bound (R-032), so the cache
  of the D-021 layout carries this candidate.
- The R-033 loops (old GGUF with the wrong gating) did not recur: both 8k answers stopped on their own.

## Gaps in Hermclaw
- Gemma 4 and GLM-4.7-Flash: none found. `enable_thinking: false` in `chat_template_kwargs` is what LiteLLM already
  sends for `think: false` profiles.
- gpt-oss-20b: a profile cannot set `reasoning_effort`. Without it the model reasons at `medium` (the model card's
  default, **unconfirmed** here, not measured). The role's preset validation (`^[A-Za-z0-9._:,/-]+$`) does not allow
  the JSON value of llama-server's `--chat-template-kwargs`, so presets cannot close the gap: part 2 needs a generic
  per-profile `chat_template_kwargs` (or `reasoning_effort`) in the model profile.

## Result
- Part 1a: Gemma 4 26B-A4B passes.
- Part 1b: gpt-oss-20b passes with `reasoning_effort: low`, which Hermclaw cannot set yet.
- Part 1c: GLM-4.7-Flash passes; prefill is about a fifth of the others'.
- Next part: 2 (per-profile `chat_template_kwargs`/`reasoning_effort`), then 3a with all three candidates.
