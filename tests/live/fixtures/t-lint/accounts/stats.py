"""Kennzahlen über Benutzerkonten (Liste von Dictionaries mit name, role, active, logins)."""

import logging

log = logging.getLogger(__name__)


def count_admins(users):
    """Anzahl der Konten mit der Rolle admin."""
    admins = [u for u in users if u.get("role") == "admin"]
    names = sorted(u["name"] for u in admins)
    log.debug("admins: %s", ", ".join(names))
    return len(admins)


def count_active(users):
    """Anzahl der aktiven Konten."""
    active = [u for u in users if u.get("active")]
    names = sorted(u["name"] for u in active)
    return len(active)


def total_logins(users):
    """Summe der Anmeldungen aller Konten (fehlende Angabe zählt als 0)."""
    total = 0
    for u in users:
        total += int(u.get("logins") or 0)
    return total


def most_active(users):
    """Name des Kontos mit den meisten Anmeldungen, bei Gleichstand alphabetisch zuerst; None bei leerer Liste."""
    if not users:
        return None
    best = max(users, key=lambda u: (int(u.get("logins") or 0), [-ord(c) for c in u["name"]]))
    return best["name"]
