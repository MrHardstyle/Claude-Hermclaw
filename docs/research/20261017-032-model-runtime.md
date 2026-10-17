# 20261017-032 – Coder model and inference runtime on the 8 GB model host (D-015)

## Question
Which model and which inference runtime make the autonomous coding loop stable and efficient on `.224`
(GTX 1080 8 GB, 6 vCPU x86-64-v3, 24 GB RAM)?

## Why needed
In live battery run 1 on 0.1.0rc14, J2–J5 failed with `REPLAN_LIMIT`. Almost every failure traced to the coder `qwen3-coder:30b`:
- green tests but no `complete_step`;
- the same check proposed again and again;
- edits whose old and new text are identical;
- output running to the 6144-token limit.

Runtime guards were added for each of these (BUG-175/179/181/182), but the symptom class kept coming back.

Coder turns averaged 39 s, of which only ~6 s was decoding: ~4.7k prompt tokens were prefilled with 68 % of the model on the CPU.
Model switches (planner ↔ coder ↔ reviewer, all exclusive) added load times on top. Runaway outputs alone cost 82 of 679 coder minutes in 24 h (12 %).

## Sources (accessed 2026-10-17)
- Ollama library, `qwen3.6` (tags 35b/27b, 256K context, tools, thinking): https://ollama.com/library/qwen3.6
- Qwen3.6-35B-A3B model card, mirrored benchmark table:
  - SWE-bench Verified 73.4 and Terminal-Bench 2.0 51.5 (Qwen3.5-27B: 75.0/41.6; Gemma 4-31B: 52.0 SWE-bench);
  - released 2026-04-16, Apache 2.0.
  - Example mirror: https://huggingface.co/servantofares/Qwen3.6-35B-A3B. The official card was not reachable in search.
- llama.cpp `--n-cpu-moe` (keep the MoE expert tensors of the first N layers in RAM; attention, dense weights and KV cache stay on the GPU):
  - llama-bench/llama-server `--help` of build b11361 (2026-10-02);
  - guide https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide.
- llama.cpp releases (no Linux CUDA binaries; third-party builds rejected as a supply-chain risk): https://github.com/ggml-org/llama.cpp/releases
- NVIDIA CUDA 12.8 devel image (CUDA 13 dropped sm_61 compilation): docker.io/nvidia/cuda:12.8.1-devel-ubuntu24.04

## Measurements on `.224` (2026-10-17, GPU otherwise idle)

Ollama 0.40.4, `num_ctx` 32768, `think` false; prompt ~10.1–10.7k tokens, up to 160 generated tokens:

| model | GPU share | prefill cold | decode | same prefix (cached) |
|---|---|---|---|---|
| qwen3-coder:30b | 32 % | 156 tok/s (72.5 s wall) | 9.1 tok/s | 8.4 s wall |
| qwen3.6:35b | 21 % | 154 tok/s (75.3 s wall) | 13.4 tok/s | 7.3 s wall |

llama.cpp b11361 (CUDA sm_61, `-ngl 99 -fa 1 -t 6`), `llama-bench -p 4096 -n 128 -d 8192`:

| model | `--n-cpu-moe` | pp4096 @ d8192 | tg128 @ d8192 |
|---|---|---|---|
| qwen3-coder:30b (48 layers) | 48 / 38 / 34 | 198 / 215 / 221 tok/s | 18.5 / 21.9 / 22.5 tok/s |
| qwen3.6:35b (40 layers) | 40 / 34 / 30 | 244 / 266 / 283 tok/s | 25.4 / 28.4 / 30.3 tok/s |

At 28K depth, `--n-cpu-moe 26` fails to allocate the KV cache (CUDA OOM). 30 is the setting for 32K context.

llama-server (same GGUF as Ollama's blob, `-c 32768 --n-cpu-moe 30 -fa on --jinja`, `cache_prompt`):
- the cold 10.7k prompt took 39.4 s wall (289 tok/s prefill, 29.8 tok/s decode);
- the same prefix with a changed tail took 4.6 s wall (516 new tokens prefilled);
- memory: 7.4 GB VRAM, and the GGUF is mmapped (15.9 GB RSS from page cache, 1.5 GB anonymous).

Path Hermclaw gateway → LiteLLM 1.x (`openai/` provider, `extra_body.chat_template_kwargs.enable_thinking: false`) → llama-server:
- `response_format` with a strict JSON schema is enforced by llama.cpp's grammar;
- reasoning content is 0 characters.

Invalid JSON and runaway outputs to `max_tokens` therefore cannot occur for structured calls.

## Conclusion
- **Runtime:** llama.cpp with `--n-cpu-moe` is ~2x Ollama's layer split for both MoE models: wall time of a cold coder turn 39 s vs 75 s, decode 30 vs 14 tok/s. It also adds grammar-constrained structured output.
- **Model:** qwen3.6:35b is the strongest candidate that fits: SWE-bench Verified 73.4 vs ~51 for Qwen3-Coder-30B-A3B. On llama.cpp it is faster than the current coder.
- **Memory:** the mmapped 20 GB GGUF plus 7.4 GB VRAM leave no room for a second large model. The efficient layout is one resident model for all chat roles: no model switches. Embeddings stay on Ollama (0.6 GB).
- **Integration (0.1.0rc16):**
  - profile `backend: llamacpp`;
  - LiteLLM `openai/` deployment;
  - gateway without Ollama options;
  - doctor check `llamacpp:<host>`;
  - role ollama installs and runs `hermclaw-llamacpp.service` next to Ollama, behind the same firewall table;
  - `scripts/ops/build-llamacpp.sh` builds the binaries.
- **Rollback:** delete the `models` override and set `ollama_llamacpp_enabled: false`. Ollama and the previous profiles stay installed.
- **Decision rule:** the battery decides whether the switch stays (success, repeats, duration vs run 1 on rc14).

## Evidence after the switch (battery run 1 on 0.1.0rc16, 2026-10-17)
- Report: `/var/lib/hermclaw/acceptance/20261017T142643Z-jobs.md`.
- Coder turns: 67 calls, mean 14.9 s, max 36.3 s, no failed call. On rc14 the mean was 39 s.
- Review calls: mean 19.3 s.
- Replans: mean 35.9 s.
- Planner: 29–95 s.

Durations, J1–J6 together 36 min:

| Job | Duration | Notes |
|---|---|---|
| J1 | 3.3 min | |
| J2 | 4.7 min | |
| J3 | 10.1 min | |
| J4 | 12.4 min | rc14: 35 min |
| J5 | 2.7 min | |
| J6 | 2.9 min | rc14: cancelled after 92 min |

- **Outcome:** With the corrected metric (BUG-185), J1/J2/J3/J6 succeeded with a merge request.
  - J4 burnt its replans instead of asking the user (BUG-187).
  - J5's planner answer listed invented files and repeated itself in the repairs (BUG-188).
  - Both are runtime fixes in 0.1.0rc17. No failure traced back to the model swap itself.
- **New model habit:** qwen3.6 guessed `cd /home/user/repo` in about 11 % of its commands (BUG-186, contract hint).
- **Decision:** The switch stays (D-016).
