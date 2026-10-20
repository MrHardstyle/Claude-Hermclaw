"""A3: CSV evaluation – every rule of the task statement, including the bad-row cases K1 crashed on (cycle 3)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = "zeitpunkt,sensor,wert\n"


def run(tmp_path, content=None, name="m.csv"):
    path = tmp_path / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    proc = subprocess.run([sys.executable, "messwerte.py", str(path)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def test_statistics_per_sensor_sorted_with_two_decimals(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1,b,1\nt2,a,2.5\nt3,b,2\nt4,a,-1\nt5,b,4.333\n")
    assert rc == 0, err
    assert out.splitlines() == [
        "a: anzahl=2 min=-1.00 max=2.50 mittel=0.75",
        "b: anzahl=3 min=1.00 max=4.33 mittel=2.44",
    ]


def test_missing_column_is_a_warning_not_a_crash(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1,a,1\nt2,a\nt3,a,3\n")
    assert rc == 0, err
    assert out.splitlines() == ["a: anzahl=2 min=1.00 max=3.00 mittel=2.00"]
    assert "Warnung: Zeile 3" in err


def test_non_numeric_and_empty_values_are_warned_with_their_line(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1,a,abc\nt2,a,\nt3,a,5\n")
    assert rc == 0, err
    assert out.splitlines() == ["a: anzahl=1 min=5.00 max=5.00 mittel=5.00"]
    assert "Warnung: Zeile 2" in err and "Warnung: Zeile 3" in err
    assert "Zeile 4" not in err


def test_too_many_columns_is_a_bad_row(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1,a,1,extra\nt2,a,2\n")
    assert rc == 0, err
    assert out.splitlines() == ["a: anzahl=1 min=2.00 max=2.00 mittel=2.00"]
    assert "Warnung: Zeile 2" in err


def test_spaces_around_values_are_ignored(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1, a , 1.5 \n")
    assert rc == 0, err
    assert out.splitlines() == ["a: anzahl=1 min=1.50 max=1.50 mittel=1.50"]


def test_empty_file_and_header_only(tmp_path):
    for content in ("", HEADER):
        rc, out, err = run(tmp_path, content)
        assert rc == 0, err
        assert out.strip() == "Keine gültigen Messwerte."


def test_only_bad_rows(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1,a,x\n")
    assert rc == 0 and out.strip() == "Keine gültigen Messwerte." and "Warnung: Zeile 2" in err


def test_missing_file_exits_2_with_a_message(tmp_path):
    assert (ROOT / "messwerte.py").is_file()
    rc, out, err = run(tmp_path, None, name="fehlt.csv")
    assert rc == 2 and err.strip() and "Traceback" not in err


def test_no_traceback_anywhere(tmp_path):
    rc, out, err = run(tmp_path, HEADER + "t1\n,,\nt2,a,1\n\nt3,,2\n")
    assert rc == 0 and "Traceback" not in err
    assert out.splitlines() == ["a: anzahl=1 min=1.00 max=1.00 mittel=1.00"]


def test_own_tests_exist_and_pass():
    tests = [p for p in (ROOT / "tests").rglob("test_*.py")] if (ROOT / "tests").is_dir() else []
    assert tests, "the task asks for tests"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-1500:]
