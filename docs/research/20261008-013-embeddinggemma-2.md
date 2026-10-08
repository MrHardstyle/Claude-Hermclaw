# 20261008-013 – EmbeddingGemma 2 (740M)

## Sources
- MarkTechPost, 2026-10-06: https://www.marktechpost.com/2026/10/06/google-deepmind-releases-embeddinggemma-2-a-740m-open-multimodal-embedding-model-built-on-gemma-4/
- Ollama Library: https://ollama.com/library/embeddinggemma-2:740m
- Google EmbeddingGemma Doku: https://ai.google.dev/gemma/docs/embeddinggemma

## Relevant facts
- Veröffentlichung 2026-10-06, Apache 2.0, multimodal; Konfigurationen 270M (Text) bis 740M (alle Modalitäten), gemeinsamer Vektorraum.
- 768 Dimensionen, 8.192 Token Kontext, >100 Sprachen; MTEB-Code deutlich verbessert.
- Ollama-Tag `embeddinggemma-2:740m` (~1,3 GB), Varianten bf16/nvfp4/mxfp8.

## Compatibility with our hardware
Klein; kann neben einem großen Modell resident bleiben.

## Decision fixed by architecture
EmbeddingGemma 2 + pgvector.

## Implementation consequences
- `vector(768)`; Chunks ≤ ~1.500 Token; Embedding über LiteLLM-Alias `embedding` (`/v1/embeddings`), Fallback direkt `/api/embed` über Model-Worker.
- Modellwechsel = neuer `embedding_model`-Wert pro Chunk → Re-Index.

## Open risks
Sehr neues Modell (2 Tage alt) – Serving-Stabilität vor Ort prüfen.
