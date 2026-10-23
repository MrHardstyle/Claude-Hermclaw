"""Entry point for the task manager application."""
import os
import sys

# Add the parent directory to the path so we can import tasks_app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tasks_app.app import create_app
from http.server import HTTPServer

def main():
    port = int(os.environ.get("PORT", 8080))
    app = create_app()
    server = HTTPServer(("127.0.0.1", port), app)
    print(f"Server running on http://127.0.0.1:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
