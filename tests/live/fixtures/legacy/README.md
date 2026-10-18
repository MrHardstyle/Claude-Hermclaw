# ledger

Kleines Werkzeug eines früheren Teams: liest Buchungen aus einer CSV-Datei (`datum;konto;betrag;text`, Datum
`TT.MM.JJJJ`, Betrag mit Dezimalkomma) und erstellt Kontosalden und einen Monatsbericht.

    python -m ledger data/buchungen.csv

Tests: `python -m unittest discover -s tests`
