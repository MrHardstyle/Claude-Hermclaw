"""Generate BUILD_PLAN.md skeleton from the binding Bauplan (section 42)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "Hermclaw_Next_Vollstaendiger_Bauplan_v2.md"


def section_list(text: str, header: str) -> list[str]:
    m = re.search(rf"^# {re.escape(header)}.*?$(.*?)(?=^# )", text, re.S | re.M)
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        line = line.strip()
        mm = re.match(r"^(?:-|\d+\.)\s+(.*)$", line)
        if mm:
            items.extend(x.strip() for x in re.split(r"(?<=\.)(?=\d+\.)", mm.group(1)) if x.strip())
    return items


def main() -> None:
    text = PLAN.read_text(encoding="utf-8")
    body = text.split("# 42. BUILD-PHASEN", 1)[1].split("# 43.", 1)[0]
    failure = section_list(text, "41. FAILURE-INJECTION")
    generalization = section_list(text, "40. GENERALIZATION E2E MATRIX")
    out = ["# BUILD PLAN", "", "Verbindlicher Fortschrittsplan für Hermclaw Next (generiert aus Bauplan §42, Status manuell gepflegt).", "",
           "Legende: `[x]` erledigt mit Evidence · `[~]` erledigt, Live-Verifikation auf Zielhost blockiert (BLOCKER-001) · `[ ]` offen", ""]
    for block in re.split(r"^## ", body, flags=re.M)[1:]:
        lines = block.strip().splitlines()
        title = lines[0].strip()
        m = re.match(r"PHASE (\d+) – (.*)", title)
        if not m:
            continue
        num, name = int(m.group(1)), m.group(2)
        out.append(f"## P{num:02d} – {name}")
        out.append("")
        steps = []
        for line in lines[1:]:
            mm = re.match(r"^(\d+\.\d+)\s+(.*?)\s*$", line.strip())
            if mm:
                steps.append((mm.group(1), mm.group(2).rstrip()))
        if num == 35:
            steps = [(f"35.{i}", s) for i, s in enumerate(failure, 1)]
        if num == 37:
            steps = [(f"37.{i}", s) for i, s in enumerate(generalization, 1)]
        for sid, s in steps:
            out.append(f"- [ ] {sid} {s} — Evidence: –")
        out.append("")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
