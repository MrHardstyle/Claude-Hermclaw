"""``python -m worker.model`` – run the model worker daemon (host ``.224``)."""

from __future__ import annotations

import sys

from hermclaw.contracts.common import WorkerKind
from hermclaw.core.errors import HermclawError
from worker.common.log_setup import configure_daemon_logging
from worker.common.server import run_daemon
from worker.common.settings import WorkerDaemonSettings


def main() -> int:
    try:
        settings = WorkerDaemonSettings.from_env(default_kind=WorkerKind.model)
        configure_daemon_logging(settings)
        from worker.model.app import create_app

        app = create_app(settings)
    except HermclawError as exc:
        print(f"hermclaw model worker: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    run_daemon(app, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
