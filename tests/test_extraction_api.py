import asyncio
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.extraction.events import stream_events
from backend.app.extraction.jobs import JobManager
from backend.app.extraction.service import ExtractionError
from backend.app.main import create_app
from fastapi.testclient import TestClient

PDF = b"%PDF-1.4\nfixture"  # API validation fixture; real parsing is covered by CLI tests.


def successful(args, progress, **kwargs):
    progress({"stage": "rendering", "page": 1, "total_pages": 1})
    progress({"stage": "inference"})
    assert kwargs["document_name"] == "purchase.pdf"
    return {"total_pages": 1, "result": {"purchase_orders": [{"po_number": "PO-001"}]}}


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.settings = Settings(upload_dir=self.directory, heartbeat=0.02)

    def tearDown(self):
        self.temp.cleanup()

    def client(self, extractor=successful, **settings):
        return TestClient(create_app(replace(self.settings, **settings), extractor))

    def upload(self, client, filename="purchase.pdf", content=PDF, mime="application/pdf"):
        return client.post("/api/v1/extractions", files={"file": (filename, content, mime)})

    def test_upload_sse_result_and_replay(self):
        with self.client() as client:
            response = self.upload(client)
            self.assertEqual(response.status_code, 202)
            body = response.json()
            stream = client.get(body["events_url"])
            self.assertTrue(stream.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(stream.headers["x-accel-buffering"], "no")
            self.assertIn("event: queued", stream.text)
            self.assertIn('"stage": "inference"', stream.text)
            self.assertIn("event: completed", stream.text)
            snapshot = client.get(body["status_url"]).json()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["result"]["purchase_orders"][0]["po_number"], "PO-001")
            replay = client.get(body["events_url"], headers={"Last-Event-ID": "1"}).text
            self.assertNotIn("event: queued", replay)
            self.assertIn("event: completed", replay)
            last = max(int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: "))
            self.assertNotIn("event:", client.get(body["events_url"], headers={"Last-Event-ID": str(last)}).text)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_validation_and_cleanup(self):
        with self.client(max_upload_bytes=32) as client:
            cases = [("file.txt", PDF, "application/pdf", 415),
                     ("purchase.pdf", PDF, "text/plain", 415),
                     ("purchase.pdf", b"", "application/pdf", 400),
                     ("purchase.pdf", b"not pdf", "application/pdf", 400),
                     ("purchase.pdf", PDF * 10, "application/pdf", 413)]
            for filename, content, mime, expected in cases:
                with self.subTest(filename=filename, content=content):
                    self.assertEqual(self.upload(client, filename, content, mime).status_code, expected)
                    self.assertEqual(list(self.directory.iterdir()), [])
                    self.assertEqual(client.app.state.jobs.reserved, 0)
            self.assertEqual(client.post("/api/v1/extractions").status_code, 422)

    def test_filename_paths_removed(self):
        with self.client() as client:
            body = self.upload(client, "../../purchase.pdf").json()
            client.get(body["events_url"])
            self.assertEqual(client.get(body["status_url"]).json()["filename"], "purchase.pdf")

    def test_failed_job_is_terminal_and_removes_upload(self):
        def failure(*args, **kwargs):
            raise ExtractionError("Ollama timeout at secret/local/path")
        with self.client(failure) as client:
            with self.assertLogs("backend.app.extraction.jobs", level="ERROR"):
                body = self.upload(client).json()
                stream = client.get(body["events_url"]).text
            self.assertIn("event: failed", stream)
            self.assertNotIn("secret/local/path", stream)
            self.assertEqual(client.get(body["status_url"]).json()["status"], "failed")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_bad_ids_and_cursor(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/v1/extractions/unknown").status_code, 404)
            self.assertEqual(client.get("/api/v1/extractions/unknown/events").status_code, 404)
            body = self.upload(client).json()
            client.get(body["events_url"])
            for value in ("bad", "-1", "999"):
                self.assertEqual(client.get(body["events_url"], headers={"Last-Event-ID": value}).status_code, 400)

    def test_capacity_and_health_while_working(self):
        gate = threading.Event()
        started = threading.Event()
        def waiting(*args, **kwargs):
            started.set()
            gate.wait(5)
            return successful(*args, **kwargs)
        with self.client(waiting, queue_size=1) as client:
            try:
                first = self.upload(client).json()
                self.assertTrue(started.wait(2))
                second = self.upload(client).json()
                rejected = self.upload(client)
                self.assertEqual(rejected.status_code, 503)
                self.assertEqual(rejected.headers["retry-after"], "15")
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get(first["status_url"]).json()["status"], "processing")
                self.assertEqual(client.get(second["status_url"]).json()["status"], "queued")
            finally:
                gate.set()
            self.assertIn("event: completed", client.get(first["events_url"]).text)
            self.assertIn("event: completed", client.get(second["events_url"]).text)

    def test_retention_and_max_jobs(self):
        with self.client(max_jobs=1, retention=0.1) as client:
            body = self.upload(client).json()
            client.get(body["events_url"])
            self.assertEqual(self.upload(client).status_code, 503)
            time.sleep(0.12)
            self.assertEqual(client.get(body["status_url"]).status_code, 404)
            self.assertEqual(self.upload(client).status_code, 202)

    def test_invalid_settings(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                replace(self.settings, heartbeat=value)


class AsyncJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_disconnect_and_late_result(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = threading.Event()
            def waiting(*args, **kwargs):
                gate.wait(5)
                return successful(*args, **kwargs)
            manager = JobManager(Settings(upload_dir=Path(directory)), waiting)
            await manager.start()
            path = Path(directory) / "input.pdf"
            path.write_bytes(PDF)
            manager.reserve()
            job = manager.submit(path, "purchase.pdf")
            manager.reserved -= 1
            stream = stream_events(job, 0, 0.02)
            try:
                for _ in range(10):
                    if "event: heartbeat" in await anext(stream):
                        break
                else:
                    self.fail("Heartbeat not received while extraction was blocked")
                await stream.aclose()  # Subscriber disconnects; extraction must continue.
                gate.set()
                await asyncio.wait_for(manager.queue.join(), 2)
                self.assertEqual(job.status, "completed")
                self.assertFalse(path.exists())
                replay = "".join([event async for event in stream_events(job, 0, 0.02)])
                self.assertIn("event: completed", replay)
            finally:
                gate.set()
                await manager.close()

    async def test_shutdown_cleans_queued_files(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(Settings(upload_dir=Path(directory)), successful)
            await manager.start()
            path = Path(directory) / "pending.pdf"
            path.write_bytes(PDF)
            manager.submit(path, "purchase.pdf")
            await manager.close()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
