# 20261021-034 – llama.cpp tuning on the 8 GB model host (Paket C)

## Question
Which llama.cpp b11696 settings make prefill and decode of the resident coder qwen3.6:35b and of the planner
qwen3.8:27b on `.224` (GTX 1080 8 GB, 6 vCPU AVX2, 23 GB RAM) faster without a loss in quality?

## Why needed
- Coder turns are prefill-bound: R-032 measured ~10k-token prompts at 39.4 s cold.
- The planner qwen3.8:27b (R-033, dense, split between GPU and CPU by `--fit`) decodes at ~2 tok/s. One plan takes
  ~10–12 min. In the ladder on rc64/rc65 the planner took 15–29 of 40 minutes.
- The repository never set `-b`/`-ub`, so b11696's defaults `-b 2048 -ub 512` applied. The inventory overrides
  nothing (`ollama_llamacpp_extra_args: []`, planner `settings: {fit: "on"}`).

## Sources (accessed 2026-10-21)
- `llama-bench --help` and `llama-server --help` of b11696 on `.224`. These confirm the option names `-ub`, `-ncmoe`,
  `-fitt`, `-fitc`, `-d`, `--moe-cache-mib`, `--spec-type {…,draft-mtp,…,ngram-mod,…}`, `--spec-draft-n-ubatch` and
  `--spec-ngram-mod-n-min/-max`.
- GGUF headers of the production blobs, read on `.224`:
  - qwen3.6 has `blk.40.nextn.{eh_proj,enorm,hnorm,shared_head_norm}` and `qwen35moe.nextn_predict_layers`.
  - qwen3.8 has `blk.64.nextn.*` and `qwen35.nextn_predict_layers`.
  - Both ship an MTP layer.
- llama.cpp `tools/server/README.md`, "Model presets": in router mode the router's command-line arguments take
  precedence over every preset section. This is **unconfirmed** here (taken from the package guide) and was not needed
  for any measurement.
- R-032 for plausibility only (b11361: pp4096@d8192 283 tok/s, tg128 30.3 tok/s).

## Method
All measurements ran as root on `.224` in stop windows. The script for each window was a transient unit that
stopped the resident server, ran the tools as `ollama`, and restarted it on stop (ExecStopPost) or after the window
plus 15 min (safety timer). Before each window: no active job, no tournament or ladder. After each window: unit
active, `NRestarts` unchanged (0 throughout), no test process left on the GPU, and a chat to qwen3.6:35b answered.

```bash
# on .225: refuse with active jobs or a running tournament/ladder, then on .224:
systemd-run --unit=hc-tune-safety-<stamp> --on-active=<window+15>min /bin/systemctl start hermclaw-llamacpp.service
systemd-run --unit=hc-tune-<stamp> -p RuntimeMaxSec=<window>min \
  -p ExecStopPost='/bin/systemctl start hermclaw-llamacpp.service' /bin/bash /var/tmp/hc-tune/<block>.sh
# <block>.sh: systemctl stop hermclaw-llamacpp.service; `ollama stop` for loaded Ollama models; nvidia-smi empty;
#   sudo -u ollama /opt/llamacpp/current/bin/llama-bench -fa 1 -t 6 -r 3 -o csv … (one call per -ncmoe value, one depth)
#   or a test llama-server on 127.0.0.1:18090 (-c 32768 --jinja, like production) driven by bench_server.py
```

Scripts, prompts, raw CSVs and logs stay under `/var/tmp/hc-tune/` on `.224`. Root path: option (b), a temporary
`/etc/sudoers.d/90-hermclaw-tuning` for `kiworker`, created by the operator.

## Results

### qwen3.6:35b, llama-bench (`-ngl 99 -fa 1 -t 6 -r 3`, tok/s, mean of 3)

| `-ncmoe` | `-ub` | pp4096 @ d8192 | tg128 @ d8192 | pp4096 @ d24576 | tg128 @ d24576 |
|---|---|---|---|---|---|
| 30 | 512 (today) | 287.1 | 27.67 | 236.1 | 26.52 |
| 30 | 1024 | 414.5 | – | 319.7 | 26.67 |
| 30 | 2048 | CUDA OOM (2020 MiB compute buffer) | | | |
| 28 | 512 | 295.3 | 29.39 | – | – |
| 28 | 1024 | CUDA OOM | | | |
| 32 | 512 | 277.0 | 27.36 | – | – |
| 32 | 1024 | 404.9 | 27.36 | – | – |
| 32 | 2048 | 543.0 | 27.36 | 391.7 | 25.65 |
| 31 | 2048 | – | – | CUDA OOM | |
| 34 | 4096 (`-b 4096`) | – | – | CUDA OOM | |

`-b`/`-ub` do not change decode; the tg128 at d8192 is one call per `-ncmoe` value.

### qwen3.6:35b, test server at 32K (median of 3; cold ≈ 9,982-token prompt, follow-up turn in the D-021 layout)

| Variant | cold prefill tok/s | cold decode tok/s | cold wall | follow-up `cache_n` / prefill tokens | follow-up wall | follow-up decode |
|---|---|---|---|---|---|---|
| today: `--n-cpu-moe 30` | 266.9 | 28.70 | 40.6 s | 9946 / 47 | 3.14 s | 29.87 |
| **`--n-cpu-moe 30 -ub 1024`** | **399.8** | 29.89 | **27.3 s** | 9946 / 47 | 3.04 s | 29.81 |
| `--n-cpu-moe 32 -ub 2048` | 533.1 | 27.58 | 21.2 s | 9946 / 47 | 2.86 s | 27.26 |
| MTP `n-max 2`, `--n-cpu-moe 30 -ub 1024` | fails to load: CUDA OOM (64 MiB) | | | | | |
| MTP `n-max 2`, `--n-cpu-moe 32 -ub 1024` | 374.4 | 32.59 | 29.2 s | 9946 / 47 | 3.05 s | 27.61 |
| MTP `n-max 3`, `--n-cpu-moe 32 -ub 1024` | 374.6 | 30.85 | 28.7 s | 9946 / 47 | 3.35 s | 30.41 |
| n-gram (`ngram-mod`, n 8–24), 30/1024 | 399.0 | 27.55 | 27.5 s | 9946 / 47 | 3.31 s | 25.19 |
| `--moe-cache-mib 768`, 32/1024 | 394.4 | 20.17 | 28.6 s | 9946 / 47 | 3.93 s | 20.17 |

- The D-021 cache checkpoint works in every variant, including MTP: the follow-up turn processes 47 of 9,993 tokens.
- MTP draft acceptance on the coder is 57–68 %, mean accepted length 2.7–3.1.
- The 5 fixed prompts with the gateway body (`response_format` json_schema CoderAction, `enable_thinking: false`,
  `temperature: 0.2`) gave schema-valid answers in every variant. The actions (tool and `args.path`) matched the
  baseline: read_file calc.py, write_file calc.py, run_test, complete_step, find_text.

### qwen3.8:27b (planner)

llama-bench `-fitt 1024 -fitc 32768 -fa 1 -t 6 -r 3 -p 4096 -n 128 -d 8192`:

| `-ub` | pp4096 | tg128 |
|---|---|---|
| 512 (today) | 130.1 | 2.05 |
| 1024 | 150.5 | 1.94 |
| 2048 | 161.2 | 1.81 |

A larger ubatch enlarges the compute buffer, so fit keeps fewer layers on the GPU and decode drops. The planner's time
goes to decode, so `-ub` stays at 512.

Test server at 32K with `--fit on`, median of 3, same prompts as the coder:

| Variant | cold prefill | cold decode | follow-up decode | cold wall | follow-up wall |
|---|---|---|---|---|---|
| today | 134.3 | 1.98 | 1.93 | 117.3 s | 47.2 s |
| MTP `n-max 2` | 122.1 (−9 %) | 2.45 (+24 %) | 2.76 (+43 %) | 133.0 s | 39.8 s |
| MTP `n-max 3` | 119.2 (−11 %) | 2.52 (+28 %) | 2.93 (+52 %) | 119.4 s | 32.2 s |

MTP draft acceptance on the planner is 70–88 %, mean accepted length 3.1–3.6. The schema request was valid
(read_file calc.py).

### Load test (step 3, criterion 5)
The test ran on a test router: production `models.ini` plus `ubatch-size = 1024` for qwen3.6, `--models-max 1`,
port 18090. It ran for 30 min with requests one after another:
- 26 requests with 20,866–27,657 prompt tokens, every third with the CoderAction json_schema;
- median prefill 372 tok/s, decode 25.8 tok/s;
- after 10 min one planner request (2.52 tok/s) and back to qwen3.6, plus one Ollama embedding (768 dimensions).

Afterwards the router was alive, no child had exited with an error, the log had no `out of memory` or `CUDA error`,
and the production `NRestarts` stayed at 0.

One error came from the test script: its `max_tokens: 64` cut a schema answer mid-string (request 12). The other 8
schema answers parsed.

## Findings during the package
- **BUG-313:** an embedding call in the gap of a planner→coder swap let Ollama put `embeddinggemma` on the free GPU
  (587 MiB). qwen3.6 then failed every load with CUDA OOM, and ladder A7#2 on rc66 ended at 1/31. Ollama on `.224`
  now runs with `CUDA_VISIBLE_DEVICES=-1`, so embeddings stay on the CPU. Rolled out and verified: `library=cpu`,
  `100% CPU`.
- **BUG-314:** the ollama role restarted Ollama before `daemon-reload` (handler-name collision). The restart handlers
  now reload systemd themselves.

## Result
- **Adopted for the coder: `ubatch-size: 1024`** via `ollama_llamacpp_model_settings` (D-022, see Adoption below).
  - Cold 10k prefill +50 % (40.6 s → 27.3 s).
  - pp4096 +35 % at depth 24K.
  - Decode unchanged (+0.6 % to +4 %).
  - Follow-up turn unchanged.
- **Not adopted:**
  - `--n-cpu-moe 32 -ub 2048`: the fastest prefill (+66 to +100 %), but decode drops 3.3–3.9 % (8.7 % on the
    follow-up turn), beyond the 3 % limit.
  - MTP on the coder: it needs `--n-cpu-moe 32` to fit, then loses prefill and gains only 3–9 % decode.
  - n-gram speculation: no gain; the follow-up decode is 16 % slower.
  - `--moe-cache-mib 768`: decode −30 %.
  - A larger `-ub` for the planner: decode −5 to −12 %.
- **Open for the operator: MTP for the planner** (`spec-type: draft-mtp`, `spec-draft-n-max: 3` in its
  `ollama_llamacpp_models[].settings`).
  - Decode is +28–52 %, prefill −11 %, so it misses the 3 % rule.
  - For a typical plan (~4k prompt tokens, ~1,300 output tokens) the extra prefill costs ~3.5 s and the faster
    decode saves ~3–4 min.
  - The rule was written for a uniform loss, so the decision goes to the operator.

## Adoption (D-022)
- **Change:**
  - `ollama_llamacpp_model_settings: {ubatch-size: 1024}` in the inventory, a new role variable (commit 9cad054).
  - Rolled out with `bootstrap.yml --limit model_workers --tags ollama` on 2026-10-21 20:40.
  - Diff of `models.ini`: one line `ubatch-size = 1024` in `[qwen3.6-35b]`. The unit and the Ollama drop-in are
    unchanged.
- **Checks after the rollout:**
  - `hermclaw doctor` 21 OK, 0 warn, 0 fail (`llamacpp:model-224` healthy).
  - One CoderAction schema request through LiteLLM to `coder-main`: valid (read_file calc.py).
  - `NRestarts` 0, no OOM or CUDA error in the journal.
- **Practice test** (`coder-tournament --model coder-main --task A1 --task A3 --task A5 --reps 1`, rc66):

  | Run | A1 | A3 | A5 | correct |
  |---|---|---|---|---|
  | before (`llamacpp-base-20261021`) | 4/4, 0.9 min | 10/10, 6.0 min | 5/6, 3.6 min | 2/3 |
  | after (`llamacpp-ub1024-20261021`) | 4/4, 0.7 min | 10/10, 4.0 min | 6/6, 2.4 min | 3/3 |

- **Way back:** `cp infra/ansible/inventory/hosts.yml.bak-20261021-pre-llamacpp-tuning infra/ansible/inventory/hosts.yml`,
  then the same rollout.
