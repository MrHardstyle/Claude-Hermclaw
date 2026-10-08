"""Worker protocol, orchestrator side (Bauplan §24, P07).

Modules:

- :mod:`hermclaw.workers.auth` – per-worker tokens and HMAC request signing (shared with the daemons)
- :mod:`hermclaw.workers.schemas` – API/response schemas of the worker protocol
- :mod:`hermclaw.workers.errors` – typed worker errors with stable codes
- :mod:`hermclaw.workers.registry` – worker registry, heartbeat ingest, offline detection, selection
- :mod:`hermclaw.workers.monitor` – background offline sweep
- :mod:`hermclaw.workers.client` – typed HTTP clients for the execution and model worker daemons
- :mod:`hermclaw.workers.api` – FastAPI router ``/api/workers``

This package ``__init__`` deliberately imports nothing: the worker daemons import ``auth``/``schemas``
without pulling in SQLAlchemy.
"""
