"""Shared building blocks of the worker daemons (``.222`` execution, ``.224`` model/media).

- :mod:`worker.common.settings`  – environment configuration (``WORKER_ID``, ``WORKER_KIND``, ...)
- :mod:`worker.common.auth`      – ASGI middleware verifying orchestrator-signed requests
- :mod:`worker.common.system`    – CPU/RAM/disk/load metrics from ``/proc`` and ``shutil``
- :mod:`worker.common.gpu`       – NVIDIA telemetry via ``nvidia-smi`` CSV
- :mod:`worker.common.state`     – effective worker state and active-work tracking
- :mod:`worker.common.heartbeat` – signed heartbeat sender loop
- :mod:`worker.common.server`    – base FastAPI app (auth, errors, ``/health``, lifespan), uvicorn runner
- :mod:`worker.common.log_setup` – structured JSON logging with secret redaction
- :mod:`worker.common.errors`    – daemon errors with HTTP status
"""
