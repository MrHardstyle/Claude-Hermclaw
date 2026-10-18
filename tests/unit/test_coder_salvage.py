"""BUG-252: complete lines of a write_file answer cut off at the output limit."""

from __future__ import annotations

import json

from hermclaw.coder.salvage import salvage_write

LINES = "".join(f'print("line {i} \\\\ \\"q\\" ü")\n' for i in range(25))


def _cut(args: dict[str, object], drop: int) -> str:
    return json.dumps({"tool": "write_file", "args": args})[:-drop]


def test_complete_lines_with_escapes_survive_the_cut() -> None:
    got = salvage_write(_cut({"path": "src/a b.py", "content": LINES + "print('unfinish"}, 4))
    assert got is not None and got.path == "src/a b.py" and got.content == LINES and got.lines == 25 and not got.append


def test_append_flag_and_key_order_are_kept() -> None:
    raw = '{"tool": "write_file", "args": {"append": true, "path": "x.js", "content": "' + "a();\\n" * 30 + "b("
    got = salvage_write(raw)
    assert got is not None and got.append and got.content == "a();\n" * 30


def test_a_cut_inside_an_escape_is_trimmed() -> None:
    for tail in ("\\", "\\u00", "\\u"):
        got = salvage_write('{"tool": "write_file", "args": {"path": "p.py", "content": "' + "x = 1\\n" * 21 + "y = '" + tail)
        assert got is not None and got.lines == 21, tail


def test_other_tools_and_short_parts_are_not_salvaged() -> None:
    assert salvage_write('{"tool": "replace_text", "args": {"path": "a.py", "old": "x", "new": "' + "y\\n" * 50) is None
    assert salvage_write('{"tool": "write_file", "args": {"path": "a.py", "content": "' + "y\\n" * 5) is None
    assert salvage_write('{"tool": "write_file", "args": {"content": "' + "y\\n" * 50) is None
    assert salvage_write("") is None
