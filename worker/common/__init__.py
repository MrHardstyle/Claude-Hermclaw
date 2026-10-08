"""Shared building blocks of the worker daemons (``.222`` execution, ``.224`` model/media).

- :mod:`worker.common.settings`  – environment configuration (``WORKER_ID``, ``WORKER_KIND``, ...)
- :mod:`worker.common.auth`      – ASGI middleware verifying orchestrator-signed requests
- :mod:`worker.common.system`    – CPU/RAM/disk/load metrics from ``/proc`` and ``shutil``
- :mod:`worker.common.gpu`       – NVIDIA telemetry via ``nvidia-smi`` CSV
- :mod:`worker.common.heartbeat` – signed heartbeat sender loop
- :mod:`worker.common.server`    – runtime state, base FastAPI app, uvicorn runner
- :mod:`worker.common.logging`   – structured JSON logging with secret redaction
"""
