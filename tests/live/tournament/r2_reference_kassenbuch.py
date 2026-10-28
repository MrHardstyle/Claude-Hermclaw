"""Reference implementation of ladder stage R2 (kassenbuch), only to validate the hidden check script."""

import datetime
import re
import sys
from pathlib import Path

AMOUNT = re.compile(r"^(-?)([0-9]+)(?:,([0-9]{2}))?$")
MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def usage(msg):
    print(f"Aufruf: kassenbuch DATEI [--monat JJJJ-MM]: {msg}", file=sys.stderr)
    sys.exit(3)


def fmt(cents):
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100},{c % 100:02d}"


def main(argv):
    path, month = None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--monat":
            if i + 1 >= len(argv) or not MONTH.match(argv[i + 1]):
                usage("ungültiger Monat")
            month = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            usage(f"unbekannte Option {a}")
        elif path is None:
            path = a
            i += 1
        else:
            usage("zu viele Argumente")
    if path is None:
        usage("Datei fehlt")
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        print(f"Datei nicht lesbar: {e}", file=sys.stderr)
        return 1
    lines = raw.decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sums = {}
    for no, raw_line in enumerate(lines, 1):
        line = raw_line.removesuffix("\r")
        if no == 1:
            if line != "datum;kategorie;betrag":
                print("Zeile 1: Kopfzeile muss 'datum;kategorie;betrag' sein", file=sys.stderr)
                return 2
            continue
        if line.strip() == "":
            continue
        parts = line.split(";")
        err = None
        if len(parts) != 3:
            err = "drei Felder erwartet"
        else:
            d, cat, amount = parts[0], parts[1].strip(), parts[2]
            try:
                if not DATE.match(d):
                    raise ValueError
                datetime.date.fromisoformat(d)
            except ValueError:
                err = "ungültiges Datum"
            if err is None and not cat:
                err = "leere Kategorie"
            m = AMOUNT.match(amount)
            if err is None and not m:
                err = "ungültiger Betrag"
        if err:
            print(f"Zeile {no}: {err}", file=sys.stderr)
            return 2
        cents = int(m.group(2)) * 100 + int(m.group(3) or 0)
        if m.group(1):
            cents = -cents
        if month and not d.startswith(month + "-"):
            continue
        sums[cat] = sums.get(cat, 0) + cents
    out = [f"{c}: {fmt(v)}" for c, v in sorted(sums.items(), key=lambda kv: kv[0].encode("utf-8"))]
    out.append(f"GESAMT: {fmt(sum(sums.values()))}")
    print("\n".join(out))
    return 0


sys.exit(main(sys.argv[1:]))
