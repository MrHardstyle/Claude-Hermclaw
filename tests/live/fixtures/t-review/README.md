# shop

Preisberechnung für Bestellungen (`shop/pricing.py`, Funktion `order_total(unit_price, quantity, coupon=None)`).

Regeln laut Anforderung:
- Preis = Stückpreis × Menge; Beträge werden als `Decimal` gerechnet.
- Mengenrabatt: ab 10 Stück 5 %, ab 50 Stück 10 % (nicht kumulativ).
- Gutschein `SOMMER`: 3,00 € Abzug, angewendet nach dem Mengenrabatt.
- Der Endbetrag ist nie negativ und wird kaufmännisch auf 2 Nachkommastellen gerundet (0,125 → 0,13).
- Unbekannte Gutscheine werden ignoriert.

Tests: `python -m pytest -q`
