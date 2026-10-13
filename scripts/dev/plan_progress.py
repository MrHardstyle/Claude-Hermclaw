"""Per-phase progress table from BUILD_PLAN.md (``[x]`` done, ``[~]`` done with live verification blocked, ``[ ]`` open).

Usage: python scripts/dev/plan_progress.py [--markdown]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2] / "BUILD_PLAN.md"


def progress() -> list[tuple[str, int, int, int]]:
    rows: list[tuple[str, int, int, int]] = []
    phase, counts = None, [0, 0, 0]
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## (P\d+ – .+)$", line)
        if m:
            if phase:
                rows.append((phase, *counts))
            phase, counts = m.group(1), [0, 0, 0]
            continue
        s = re.match(r"^- \[([x~ ])\] ", line)
        if s and phase:
            counts["x~ ".index(s.group(1))] += 1
    if phase:
        rows.append((phase, *counts))
    return rows


def main() -> None:
    rows = progress()
    if "--markdown" in sys.argv:
        print("| Phase | erledigt | erledigt, live offen (BLOCKER-001) | offen |")
        print("|---|---|---|---|")
        for name, x, t, o in rows:
            print(f"| {name} | {x} | {t} | {o} |")
        tx, tt, to = (sum(r[i] for r in rows) for i in (1, 2, 3))
        print(f"| **Summe** | **{tx}** | **{tt}** | **{to}** |")
    else:
        for name, x, t, o in rows:
            print(f"{name}: {x} done, {t} live-pending, {o} open")


if __name__ == "__main__":
    main()
