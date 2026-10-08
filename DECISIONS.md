# DECISIONS

Architekturentscheidungen, die innerhalb des verbindlichen Bauplans noch offen waren.

Die festgelegte Kernarchitektur aus dem Bauplan darf hier nicht eigenmächtig ersetzt werden.

## Fest vorgegeben

- Greenfield statt Weiterpatchen der alten Runtime
- PostgreSQL + pgvector als persistente Source of Truth
- Gemma 4 26B A4B IT als primärer Planner/Replanner
- Qwen3-Coder 30B als Implementation Worker
- Qwen3.8 27B als Heavy Reviewer
- Qwen3 8B als Fast Router
- EmbeddingGemma 2 für Retrieval
- FastAPI / Python Backend
- React / TypeScript / Vite Frontend
- LiteLLM + Ollama
- rootless Podman
- expliziter Scope
- deterministic Verifier
- Research mit transparenten Quellen
- Stagnation Detection
- Runtime-kontrollierte Git-Operationen
