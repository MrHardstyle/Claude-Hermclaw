"""A5: the tests no longer share a database; the store and the expected values stay as they were."""

import subprocess
import sys
from pathlib import Path

from notes.store import NoteStore

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(__file__).resolve().parent / "_base"
TESTS = "tests/test_store.py"


def _pytest(*args):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=ROOT, capture_output=True, text=True)


def _ids():
    out = _pytest("--collect-only", TESTS).stdout
    return [line.strip() for line in out.splitlines() if "::" in line]


def test_suite_passes_twice_in_a_row():
    for _ in range(2):
        run = _pytest(TESTS)
        assert run.returncode == 0, run.stdout[-2000:]


def test_suite_passes_in_reverse_order():
    ids = _ids()
    assert len(ids) >= 5
    run = _pytest(*reversed(ids))
    assert run.returncode == 0, run.stdout[-2000:]


def test_every_test_passes_alone():
    for test_id in _ids():
        run = _pytest(test_id)
        assert run.returncode == 0, test_id


def test_store_is_unchanged():
    assert (ROOT / "notes/store.py").read_bytes() == (BASE / "notes/store.py").read_bytes()


def test_expected_values_are_kept():
    text = "".join((ROOT / TESTS).read_text(encoding="utf-8").split())
    assert '["Brief schreiben"]'.replace(" ", "") in text
    assert "only_open=True)],[])" in text


def test_store_behaviour(tmp_path):
    store = NoteStore(str(tmp_path / "n.db"))
    a = store.add(" eins ")
    store.add("zwei")
    assert store.mark_done(a)
    assert [n["text"] for n in store.all(only_open=True)] == ["zwei"]
    assert [n["text"] for n in store.all()] == ["eins", "zwei"]
