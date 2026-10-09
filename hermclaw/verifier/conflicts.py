"""Merge-conflict marker scan over added lines (P21 21.8)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermclaw.verifier.languages import MARKUP_LANGUAGES, detect_language
from hermclaw.verifier.types import AddedLine

_START = re.compile(r"^<{7}(?:\s|$)")
_BASE = re.compile(r"^\|{7}(?:\s|$)")
_SEPARATOR = re.compile(r"^={7}\s*$")
_END = re.compile(r"^>{7}(?:\s|$)")


@dataclass(frozen=True)
class ConflictMarker:
    path: str
    line: int
    marker: str  # <<<<<<< | ||||||| | ======= | >>>>>>>

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "marker": self.marker}


def scan_conflicts(path: str, lines: list[AddedLine]) -> list[ConflictMarker]:
    """Markers at line start. A bare ``=======`` only counts outside markup files (heading underline) or when the
    same change also adds a start/end marker."""
    found: list[ConflictMarker] = []
    separators: list[ConflictMarker] = []
    for added in lines:
        text = added.text.rstrip("\r")
        if _START.match(text):
            found.append(ConflictMarker(path, added.line, "<<<<<<<"))
        elif _END.match(text):
            found.append(ConflictMarker(path, added.line, ">>>>>>>"))
        elif _BASE.match(text):
            found.append(ConflictMarker(path, added.line, "|||||||"))
        elif _SEPARATOR.match(text):
            separators.append(ConflictMarker(path, added.line, "======="))
    if separators and (found or detect_language(path) not in MARKUP_LANGUAGES):
        found.extend(separators)
    return sorted(found, key=lambda m: m.line)
