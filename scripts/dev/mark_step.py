"""Mark BUILD_PLAN.md steps: python scripts/dev/mark_step.py STATUS EVIDENCE STEP_ID [STEP_ID...]

STATUS: x (done), ~ (done, live verification blocked), ' ' (open).
STEP_ID may be a range like 3.1-3.9.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2] / "BUILD_PLAN.md"


def expand(ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in ids:
        m = re.match(r"^(\d+)\.(\d+)-(\d+)\.(\d+)$", i)
        if m and m.group(1) == m.group(3):
            for k in range(int(m.group(2)), int(m.group(4)) + 1):
                out.add(f"{m.group(1)}.{k}")
        else:
            out.add(i)
    return out


def main() -> None:
    status, evidence, *ids = sys.argv[1:]
    wanted = expand(ids)
    lines = PLAN.read_text(encoding="utf-8").splitlines()
    hit = set()
    for n, line in enumerate(lines):
        m = re.match(r"^- \[(.)\] (\d+\.\d+) (.*?) — Evidence: .*$", line)
        if m and m.group(2) in wanted:
            lines[n] = f"- [{status}] {m.group(2)} {m.group(3)} — Evidence: {evidence}"
            hit.add(m.group(2))
    PLAN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    missing = wanted - hit
    if missing:
        sys.exit(f"unknown steps: {sorted(missing)}")


if __name__ == "__main__":
    main()
