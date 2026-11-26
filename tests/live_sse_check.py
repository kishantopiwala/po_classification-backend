"""Real HTTP smoke test: Poppler -> delayed Ollama fixture -> replayable SSE.

Run from repository root. LIVE_INFERENCE_SECONDS=125 checks the two-minute case.
No real model is needed and no external requests are made.
"""

import json
import os
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn
from backend.app.core.config import Settings
from backend.app.main import create_app
from test_extract_pdf import make_pdf


def main():
    delay = float(os.getenv("LIVE_INFERENCE_SECONDS", "2"))

    class OllamaFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                response = {"capabilities": ["vision"]}
            else:
                time.sleep(delay)
                response = {
                    "done": True,
                    "done_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"purchase_orders": [{"po_number": "PO-LIVE"}]}
                        )
                    },
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    ollama = ThreadingHTTPServer(("127.0.0.1", 0), OllamaFixture)
    ollama_thread = threading.Thread(target=ollama.serve_forever, daemon=True)
    ollama_thread.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        pdf = folder / "purchase.pdf"
        make_pdf(pdf)
        upload_dir = folder / "uploads"
        config = Settings(
            host=f"http://127.0.0.1:{ollama.server_port}",
            upload_dir=upload_dir,
            heartbeat=0.5,
            dpi=72,
            timeout=delay + 15,
        )
        server = uvicorn.Server(uvicorn.Config(create_app(config), log_level="warning"))
        server_thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        server_thread.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as client:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(0.05)
                assert server.started, "Uvicorn did not start"
                started = time.monotonic()
                with pdf.open("rb") as file:
                    upload = client.post(
                        "/api/v1/extractions",
                        files={"file": ("purchase.pdf", file, "application/pdf")},
                    )
                assert upload.status_code == 202, upload.text
                assert time.monotonic() - started < 2, "Upload waited for inference"
                body = upload.json()
                last_id = 0
                heartbeats = 0
                # Read one heartbeat, then genuinely disconnect the TCP stream.
                with client.stream("GET", body["events_url"]) as response:
                    assert response.status_code == 200
                    for line in response.iter_lines():
                        if line.startswith("id: "):
                            last_id = int(line[4:])
                        if line == "event: heartbeat":
                            heartbeats += 1
                            break
                assert last_id > 0
                assert client.get("/health").status_code == 200
                assert client.get(body["status_url"]).json()["status"] == "processing"
                completed = False
                with client.stream(
                    "GET", body["events_url"], headers={"Last-Event-ID": str(last_id)}
                ) as response:
                    for line in response.iter_lines():
                        if line.startswith("id: "):
                            assert int(line[4:]) > last_id, (
                                "Replayed an acknowledged event"
                            )
                        if line == "event: heartbeat":
                            heartbeats += 1
                        if line == "event: completed":
                            completed = True
                assert completed
                result = client.get(body["status_url"]).json()
                assert (
                    result["result"]["purchase_orders"][0]["po_number"] == "PO-LIVE"
                ), result
                assert result["total_pages"] == 2
                assert not list(upload_dir.iterdir()), "Upload was not removed"
                elapsed = time.monotonic() - started
                assert elapsed >= delay
                print(
                    f"Live HTTP SSE passed: {elapsed:.1f}s, {heartbeats} heartbeats, disconnect/replay, final JSON, cleanup",
                    flush=True,
                )
        finally:
            server.should_exit = True
            server_thread.join(delay + 20)
            listener.close()
            ollama.shutdown()
            ollama.server_close()
            ollama_thread.join(2)
    print("VERDICT: PASS", flush=True)


if __name__ == "__main__":
    main()
