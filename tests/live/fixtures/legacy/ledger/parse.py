import csv
from datetime import datetime


def parse_amount(text):
    return float(text.replace(".", "").replace(",", "."))


def parse_date(text):
    return datetime.strptime(text, "%m.%d.%Y").date()


def read_bookings(path, bookings=[]):
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            bookings.append(
                {
                    "date": parse_date(row["datum"]),
                    "account": row["konto"],
                    "amount": parse_amount(row["betrag"]),
                    "text": row["text"],
                }
            )
    return bookings
