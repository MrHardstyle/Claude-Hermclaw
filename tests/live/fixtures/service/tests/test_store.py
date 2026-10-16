import unittest

from inventory.store import Store


class StoreTest(unittest.TestCase):
    def test_add_and_get(self) -> None:
        store = Store()
        store.add_item("apple", 3)
        store.add_item("apple", 2)
        self.assertEqual(store.get_item("apple"), 5)
        self.assertIsNone(store.get_item("pear"))

    def test_list_is_sorted(self) -> None:
        store = Store()
        store.add_item("pear", 1)
        store.add_item("apple", 2)
        self.assertEqual([i["name"] for i in store.list_items()], ["apple", "pear"])


if __name__ == "__main__":
    unittest.main()
