def balances(bookings):
    result = {}
    for b in bookings:
        result[b["account"]] = result.get(b["account"], 0) + b["amount"]
    return result


def months(bookings):
    keys = sorted({(b["date"].year, b["date"].month) for b in bookings})
    first, last = keys[0], keys[-1]
    out = []
    year, month = first
    while (year, month) < last:
        out.append((year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def monthly_report(bookings):
    lines = []
    for year, month in months(bookings):
        total = sum(b["amount"] for b in bookings if b["date"].year == year and b["date"].month == month)
        lines.append(f"{month:02d}/{year}: {total:.2f}")
    return lines
