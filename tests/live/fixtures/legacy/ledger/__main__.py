import sys

from ledger.parse import read_bookings
from ledger.report import balances, monthly_report


def main(argv):
    bookings = read_bookings(argv[1])
    for account, total in sorted(balances(bookings).items()):
        print(f"{account}: {total}")
    for line in monthly_report(bookings):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
