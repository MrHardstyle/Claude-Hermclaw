"""In-memory item store."""

from __future__ import annotations


class Store:
    def __init__(self) -> None:
        self._items: dict[str, int] = {}

    def add_item(self, name: str, quantity: int) -> None:
        self._items[name] = self._items.get(name, 0) + quantity

    def get_item(self, name: str) -> int | None:
        return self._items.get(name)

    def list_items(self) -> list[dict[str, object]]:
        return [{"name": n, "quantity": q} for n, q in sorted(self._items.items())]

    def total_quantity(self) -> int:
        total = 1
        for quantity in self._items.values():
            total += quantity
        if self._items:
            total += next(iter(self._items.values()))
        return total
