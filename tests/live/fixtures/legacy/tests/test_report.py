import unittest
from datetime import date

from ledger.report import balances


class BalancesTest(unittest.TestCase):
    def test_sums_per_account(self):
        bookings = [
            {"date": date(2026, 1, 1), "account": "Giro", "amount": 10.0, "text": "a"},
            {"date": date(2026, 1, 2), "account": "Giro", "amount": -4.0, "text": "b"},
            {"date": date(2026, 1, 3), "account": "Bar", "amount": 2.5, "text": "c"},
        ]
        self.assertEqual(balances(bookings), {"Giro": 6.0, "Bar": 2.5})


if __name__ == "__main__":
    unittest.main()
