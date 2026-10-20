from accounts.stats import count_active, count_admins, most_active, total_logins

USERS = [
    {"name": "bea", "role": "admin", "active": True, "logins": 5},
    {"name": "carl", "role": "member", "active": False, "logins": 0},
    {"name": "anna", "role": "admin", "active": False, "logins": 5},
    {"name": "dora", "role": "member", "active": True},
]


def test_count_admins():
    assert count_admins(USERS) == 2


def test_count_active():
    assert count_active(USERS) == 2


def test_total_logins():
    assert total_logins(USERS) == 10


def test_most_active_prefers_alphabetical_on_ties():
    assert most_active(USERS) == "anna"
    assert most_active([]) is None
