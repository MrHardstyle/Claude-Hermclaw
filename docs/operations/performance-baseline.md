# Performance baseline (P38)

Measured on 2026-10-13 by `tests/load/test_load.py` on the build host (container, 4 vCPU, local PostgreSQL 16,
scripted models). Raw numbers: [`performance-baseline.json`](performance-baseline.json). Re-measure on the target
hosts after deployment (P40 soak) with

```bash
HERMCLAW_PERF_OUT=/tmp/perf.json .venv/bin/pytest -q -m slow tests/load/test_load.py
```

Model inference time is **not** included (the models are scripted): on `.224` (GTX 1080, 8 GB) a coder turn of
Qwen3-Coder 30B dominates every job by orders of magnitude; these numbers describe the runtime's own overhead.

| # | Scenario | Result | Reading |
|---|---|---|---|
| 38.1 | 6 jobs (implement → test → review, real git/verifier/review pipeline), scheduler concurrency 4 | all succeeded, wall 12.6 s, job p50 11.3 s, max 12.5 s | the runtime overhead per job is ~2 s of work; jobs overlap without interference |
| 38.2 | 20 000 events appended (redaction, NOTIFY) and replayed | append ≈ 570 events/s, replay ≈ 29 000 events/s | a coder turn emits ~5–10 events: event volume is never the bottleneck; SSE replay after reconnect is instant |
| 38.3 | 5 jobs × 25 steps, 10 parallel slots, `SKIP LOCKED` leases | 125 steps in 4.0 s, tick p50 106 ms / p95 227 ms, every step exactly once | no double dispatch under contention |
| 38.4 | context build on a 1 501-file repository (repo intelligence, lexical) | inventory 3.5 s (first, cached afterwards), build cold 0.86 s, warm p50 0.83 s | per-turn context is cheap compared with a 30B turn (tens of seconds) |
| 38.5 | model worker load switches (FakeOllama) / model lease acquire+release / 8 contenders on one large-model budget | switch 3.6 ms (protocol overhead only), lease p50 22 ms, p95 31 ms, 8×50 ms holders served in 2.2 s | hand-over latency ≈ 0.27 s per waiter (poll interval 0.25 s) – negligible next to a real model switch (seconds); tune `poll_interval` if needed |
| 38.6 | Wake-on-LAN pipeline (real UDP/TCP/HTTP fakes, 0.3 s simulated boot) | wake from offline 0.41 s (+ boot time), already-ready probe 0.06 s | the controller adds ~0.1 s on top of the real boot time; the real boot of `.222`/`.224` is minutes (live: BLOCKER-001) |
| 38.7 | video job preempts a running coder lease | yield requested after 0.12 s, GPU granted 0.34 s after the request, 0.15 s after the coder released | the GPU is never shared; the coder checkpoints at its next turn boundary |
| 38.8 | peak RSS of the scheduler process running one full job | 104 MB | the orchestrator `.225` needs < 1 GB RAM for the runtime itself (PostgreSQL, LiteLLM and SearXNG come on top) |

## Limits and tuning notes

* Event append throughput is per process; the scheduler and the API write events independently.
* `ResourceManager(poll_interval=0.25)` bounds lease hand-over latency; it is the price for a database-only lease
  protocol without extra brokers.
* Repository intelligence caches the inventory per revision/status digest; the first inventory of a very large
  repository (> 20 000 files) is capped (`files_listed_truncated`) and logged.
