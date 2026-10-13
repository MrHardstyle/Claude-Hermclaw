# 20261013-025 – Re-check of the Ollama model tags before installation

## Question
Are the Ollama tags of all fixed model roles still valid on 2026-10-13, and what is the fallback when a tag is
missing on the target host `.224`?

## Why needed
INSTALLATION.md (P32.6 "Ollama models") – the operator pulls exactly these tags.

## Sources (accessed 2026-10-13)
- Ollama library tags page qwen3.8 (search index): https://ollama.com/library/qwen3.8/tags , https://ollama.com/library/qwen3.8:27b-q4_K_M
- Ollama announcement (X, 2026-08-14): https://x.com/ollama/status/2088314436088168491
- Gemma 4 + Ollama guides (secondary): https://www.verdent.ai/guides/gemma-4-ollama , https://bestllmfor.com/guide/gemma-4-en-local-ollama-installation-perfs/
- Google blog EmbeddingGemma 2 (primary): https://blog.google/innovation-and-ai/technology/developers-tools/embeddinggemma-2/
- MarkTechPost 2026-10-06: https://www.marktechpost.com/2026/10/06/google-deepmind-releases-embeddinggemma-2-a-740m-open-multimodal-embedding-model-built-on-gemma-4/
- Restriction of this build environment: ollama.com does not resolve and registry.ollama.ai is blocked by the egress proxy
  (HTTP 403), so the library pages could only be read through the search index.

## Relevant facts
| Role | Tag | Status 2026-10-13 |
|---|---|---|
| Planner | `gemma4:26b` (26B MoE, ~4B active, ~18 GB Q4, 256K ctx) | listed (secondary sources agree); Ollama ≥ 0.22 |
| Planner fallback | `gemma4:12b` (12B "Unified", ~7.6 GB) | listed since 2026-06-03 (secondary sources) |
| Coder | `qwen3-coder:30b` (MoE 30B/3.3B active, ~19 GB) | unchanged (R-011) |
| Heavy reviewer | `qwen3.8:27b` (= `qwen3.8:27b-q4_K_M`, ~18 GB, 256K ctx; also `-q8_0` 30 GB, `-bf16` 56 GB, `-mlx`) | **confirmed on the official library tags page** |
| Fast router | `qwen3:8b` | unchanged (R-010) |
| Embedding | `embeddinggemma-2:740m` | model confirmed by Google (2026-10-06, 768 dim, 8K ctx, Apache 2.0, weights on HF/Kaggle); **Ollama library tag not confirmable** from here |

## Decision
* Roles and tags stay as fixed by the architecture (CLAUDE.md: Gemma planner, Qwen3-Coder 30B worker,
  Qwen3.8 27B reviewer, Qwen3 8B router). No newer model found that would justify a change for the
  GTX 1080 8 GB + 32 GB RAM host: `qwen3.8:27b-q4_K_M` is the only reviewer build that fits.
* Embedding: install `embeddinggemma-2:740m`; when `ollama pull` reports "file does not exist", use the first
  EmbeddingGemma `embeddinggemma:300m` (also 768 dimensions, so `vector(768)` and the schema stay valid) and set
  `model:` of the `embedding` profile accordingly. A later switch re-indexes automatically (embedding_model per chunk).
* `scripts/ops/hermclaw-model-pull` implements exactly this (pull, verify with `ollama show`, fallback for the
  embedding model only – a missing *role* model is a hard error).

## Open risks
Secondary sources for the Gemma 4 sizes; verify on `.224` with `ollama show <tag>` (part of the install checklist).
