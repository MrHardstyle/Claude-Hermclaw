"""HTTP server of the inventory service."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from inventory.store import Store

STORE = Store()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/items":
            self._send(200, STORE.list_items())
        elif self.path == "/items/total":
            self._send(200, {"total": STORE.total_quantity()})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server API
        pass


def make_server(host: str = "127.0.0.1", port: int = 8080) -> HTTPServer:
    return HTTPServer((host, port), Handler)


def main() -> None:
    server = make_server("127.0.0.1", 8080)
    print("serving on 127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
