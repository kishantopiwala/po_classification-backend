"""Exercise chat through FastAPI and an actual local Ollama HTTP fixture."""

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.main import create_app
from fastapi.testclient import TestClient


class ChatTests(unittest.TestCase):
    @contextmanager
    def client(self):
        self.calls = []
        self.status = 200
        self.response = {
            "done": True,
            "message": {"role": "assistant", "content": "Hello!"},
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((self.path, payload))
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(owner.response).encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.settings = Settings(
                host=f"http://127.0.0.1:{server.server_port}",
                model="test-chat",
                timeout=2,
                upload_dir=Path(directory),
            )
            try:
                with TestClient(create_app(self.settings)) as client:
                    yield client
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_chat_forwards_history_and_configured_model(self):
        messages = [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "What can you do?"},
        ]
        with self.client() as client:
            result = client.post("/api/v1/chat", json={"messages": messages})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {
                "model": "test-chat", "done": True,
                "message": {"role": "assistant", "content": "Hello!"},
            })
            self.assertEqual(self.calls, [("/api/chat", {
                "model": "test-chat", "messages": messages,
                "stream": False, "think": False,
            })])
            self.assertEqual(client.app.state.jobs.reserved, 0)
            self.assertEqual(list(self.settings.upload_dir.iterdir()), [])

    def test_invalid_requests_never_reach_ollama(self):
        with self.client() as client:
            for body in ({}, {"messages": []}, {"messages": "Hi"},
                         {"messages": [{"role": "tool", "content": "Hi"}]},
                         {"messages": [{"role": "user", "content": " \n "}]},
                         {"messages": [{"role": "user", "content": 123}]},
                         {"messages": [{"role": "user"}]}):
                with self.subTest(body=body):
                    self.assertEqual(client.post("/api/v1/chat", json=body).status_code, 422)
            self.assertEqual(self.calls, [])

    def test_upstream_errors_are_sanitized(self):
        with self.client() as client:
            for status, response in (
                (500, {"error": "secret/local/path"}),
                (200, {"error": "secret/local/path"}),
                (200, []),
                (200, {"done": False}),
                (200, {"done": True, "message": None}),
                (200, {"done": True, "message": {"role": "assistant", "content": ""}}),
                (200, {"done": True, "message": {"role": "user", "content": "Hi"}}),
                (200, {"done": True, "done_reason": "length",
                       "message": {"role": "assistant", "content": "Partial"}}),
            ):
                with self.subTest(status=status, response=response):
                    self.status, self.response = status, response
                    with self.assertLogs("backend.app.chat", level="ERROR"):
                        result = client.post("/api/v1/chat", json={
                            "messages": [{"role": "user", "content": "Hi"}],
                        })
                    self.assertEqual(result.status_code, 502)
                    self.assertNotIn("secret", result.text)

    def test_unreachable_ollama(self):
        with self.client() as client:
            # Reserve an unserved port so connect fails without a timeout delay.
            import socket
            with socket.socket() as port:
                port.bind(("127.0.0.1", 0))
                client.app.state.settings = replace(
                    self.settings, host=f"http://127.0.0.1:{port.getsockname()[1]}"
                )
                with self.assertLogs("backend.app.chat", level="ERROR"):
                    response = client.post("/api/v1/chat", json={
                        "messages": [{"role": "user", "content": "Hi"}],
                    })
                self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
