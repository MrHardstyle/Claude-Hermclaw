"""``python -m hermclaw.backup`` – see :mod:`hermclaw.backup.cli`."""

from __future__ import annotations

import sys

from hermclaw.backup.cli import main

if __name__ == "__main__":
    sys.exit(main())
