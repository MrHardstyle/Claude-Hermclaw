import json
import threading
import unittest
import urllib.request

from inventory import server


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        server.STORE = server.Store()
        self.httpd = server.make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_list_items(self) -> None:
        server.STORE.add_item("apple", 2)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/items") as resp:
            self.assertEqual(json.loads(resp.read()), [{"name": "apple", "quantity": 2}])


if __name__ == "__main__":
    unittest.main()
