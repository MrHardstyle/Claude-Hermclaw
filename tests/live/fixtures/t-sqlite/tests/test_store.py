import os
import tempfile
import unittest

from notes.store import NoteStore

DB_PATH = os.path.join(tempfile.gettempdir(), "notes-test.db")


class NoteStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        cls.store = NoteStore(DB_PATH)

    def test_add_returns_an_id(self):
        note_id = self.store.add("Milch kaufen")
        self.assertIsInstance(note_id, int)

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add("   ")

    def test_list_contains_the_new_note(self):
        self.store.add("Brief schreiben")
        texts = [n["text"] for n in self.store.all()]
        self.assertEqual(texts, ["Brief schreiben"])

    def test_mark_done_hides_note_from_open_list(self):
        note_id = self.store.add("Rasen mähen")
        self.assertTrue(self.store.mark_done(note_id))
        self.assertEqual([n["text"] for n in self.store.all(only_open=True)], [])

    def test_delete_unknown_id_returns_false(self):
        self.assertFalse(self.store.delete(9999))


if __name__ == "__main__":
    unittest.main()
