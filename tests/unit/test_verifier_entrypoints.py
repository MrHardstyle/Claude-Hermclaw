"""Which packages the verifier starts and with which environment (BUG-309)."""

from __future__ import annotations

import json
from pathlib import Path

from hermclaw.verifier.entrypoints import (
    EntryPoint,
    entry_points,
    parse_start_output,
    start_command,
    start_environment,
    traceback_exception,
)


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def test_only_changed_packages_with_a_main_are_started(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "tasks_app/__init__.py": "",
            "tasks_app/__main__.py": "",
            "tasks_app/web/handlers.py": "",
            "src/notes_api/__main__.py": "",
            "src/notes_api/core.py": "",
            "lib/helpers.py": "",
            "tests/__main__.py": "",
            "tests/test_x.py": "",
            "my-tool/__main__.py": "",
        },
    )
    changed = [
        "tasks_app/web/handlers.py",
        "src/notes_api/core.py",
        "lib/helpers.py",
        "tests/test_x.py",
        "my-tool/__main__.py",
        "README.md",
    ]
    assert entry_points(tmp_path, changed) == [EntryPoint("notes_api", "src/notes_api"), EntryPoint("tasks_app", "tasks_app")]
    assert entry_points(tmp_path, ["lib/helpers.py"]) == []


def test_only_variables_whose_name_tells_their_meaning_are_set(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/__main__.py": (
                "import os\nfrom os import environ, getenv\n"
                "PORT = int(os.environ.get('PORT', '8080'))\nDB = os.getenv('TASKS_DB', 'tasks.db')\n"
                "def token():\n    return environ['NOTES_API_TOKEN']\n"
                "WORKERS = int(getenv('WORKERS', '4'))\nDATA = os.environ.setdefault('DATA_DIR', '/var/lib/x')\n"
                "HOST = os.environ.get('BIND_HOST')\nPATH = os.environ['PATH']\nURL = os.environ.get('DATABASE_URL')\n"
            ),
        },
    )
    env = start_environment(tmp_path, EntryPoint("app", "app"))
    assert env == {"BIND_HOST": "host", "DATA_DIR": "dir", "NOTES_API_TOKEN": "secret", "PORT": "port", "TASKS_DB": "file"}


def test_the_last_exception_of_a_traceback_decides() -> None:
    chained = (
        "Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'a'\n\nDuring handling ...\n\n"
        'Traceback (most recent call last):\n  File "/workspace/notes_api/__init__.py", line 229, in main\n'
        "    print('x', file=sys.stderr)\n           ^^^\nUnboundLocalError: cannot access local variable 'sys'\n"
    )
    assert traceback_exception(chained) == "UnboundLocalError"
    assert traceback_exception("error: NOTES_API_TOKEN is not set\n") is None
    syntax = 'Traceback (most recent call last):\n  File "a.py", line 3\n    def (\n        ^\nSyntaxError: invalid syntax\n'
    assert traceback_exception(syntax) == "SyntaxError"
    line = json.dumps({"running": False, "exit_code": 1, "stderr": chained, "env": []})
    result = parse_start_output("noise\n" + line + "\n")
    assert result is not None and result.broken and not result.running
    other = parse_start_output(
        json.dumps({"running": False, "exit_code": 1, "stderr": "Traceback (most recent call last):\nIndexError: x"})
    )
    assert other is not None and other.exception == "IndexError" and not other.broken
    assert parse_start_output("no json here") is None


def test_the_command_names_module_seconds_and_variables() -> None:
    command = start_command([".venv/bin/python"], "tasks_app", 5, {"PORT": "port", "TASKS_DB": "file"})
    assert command.startswith(".venv/bin/python -c ") and command.endswith(" tasks_app 5 PORT=port TASKS_DB=file")
