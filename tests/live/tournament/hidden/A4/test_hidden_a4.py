"""Hidden checks of task A4 (inherited ledger tool with four reported bugs)."""

import importlib
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

ROOT = Path.cwd()
EXPECTED = ["Bar: -35.50", "Giro: 3209.80", "01/2026: 1140.10", "02/2026: 1234.20", "03/2026: 800.00"]


def run_cli(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "ledger", path], capture_output=True, text=True, timeout=60, cwd=ROOT)


def csv_file(tmp_path: Path, rows: str) -> str:
    p = tmp_path / "b.csv"
    p.write_text("datum;konto;betrag;text\n" + textwrap.dedent(rows).strip() + "\n", encoding="utf-8")
    return str(p)


@pytest.fixture
def ledger():
    for name in [m for m in sys.modules if m == "ledger" or m.startswith("ledger.")]:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT))
    try:
        yield importlib.import_module("ledger.parse"), importlib.import_module("ledger.report")
    finally:
        sys.path.remove(str(ROOT))


def test_the_shipped_file_gives_the_expected_report():
    res = run_cli("data/buchungen.csv")
    assert res.returncode == 0, res.stderr[-2000:]
    assert res.stdout.split() == " ".join(EXPECTED).split(), res.stdout


def test_dates_are_day_month_year(ledger):
    parse, _ = ledger
    assert parse.parse_date("31.03.2026") == date(2026, 3, 31)
    assert parse.parse_date("03.01.2026") == date(2026, 1, 3)


def test_amounts_keep_decimal_comma_and_thousands(ledger):
    parse, _ = ledger
    assert round(float(parse.parse_amount("1.250,00")), 2) == 1250.00
    assert round(float(parse.parse_amount("-0,10")), 2) == -0.10


def test_balances_print_two_decimals(tmp_path):
    res = run_cli(csv_file(tmp_path, """
        01.01.2026;Giro;0,10;a
        02.01.2026;Giro;0,20;b
        03.01.2026;Bar;1.000,00;c
    """))
    assert res.returncode == 0, res.stderr[-2000:]
    lines = res.stdout.splitlines()
    assert "Giro: 0.30" in lines and "Bar: 1000.00" in lines, res.stdout


def test_the_last_month_is_reported(tmp_path):
    res = run_cli(csv_file(tmp_path, """
        15.11.2025;Giro;10,00;a
        15.01.2026;Giro;5,00;b
    """))
    assert res.returncode == 0, res.stderr[-2000:]
    months = [line for line in res.stdout.splitlines() if line[:2].isdigit()]
    assert months[0] == "11/2025: 10.00" and months[-1] == "01/2026: 5.00", res.stdout


def test_a_single_month_is_reported(tmp_path):
    res = run_cli(csv_file(tmp_path, "05.04.2026;Giro;7,50;a"))
    assert res.returncode == 0, res.stderr[-2000:]
    assert "04/2026: 7.50" in res.stdout.splitlines(), res.stdout


def test_reading_twice_does_not_double(ledger, tmp_path):
    parse, report = ledger
    path = csv_file(tmp_path, """
        01.01.2026;Giro;10,00;a
        02.01.2026;Bar;2,50;b
    """)
    first = parse.read_bookings(path)
    second = parse.read_bookings(path)
    assert len(first) == 2 and len(second) == 2
    assert {k: round(float(v), 2) for k, v in report.balances(second).items()} == {"Giro": 10.0, "Bar": 2.5}


def test_input_data_unchanged():
    assert (ROOT / "data/buchungen.csv").read_bytes() == (ROOT / "_hidden/_base/data/buchungen.csv").read_bytes()


def test_own_tests_cover_the_corrections_and_pass():
    res = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"], capture_output=True, text=True, timeout=300, cwd=ROOT
    )
    assert res.returncode == 0, res.stderr[-3000:]
    ran = int(res.stderr.split("Ran ")[1].split()[0]) if "Ran " in res.stderr else 0
    assert ran >= 5, f"only {ran} tests: the job asks for a test per correction"
