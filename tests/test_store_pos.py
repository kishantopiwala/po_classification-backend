"""Batch import behavior without requiring PostgreSQL or Ollama."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.core.config import Settings
from backend.app.extraction.service import ExtractionError
from backend.store_pos import store_folder


class StorePOTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_skips_duplicates_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("a.pdf", "b.PDF", "c.pdf"):
                (folder / name).write_bytes(b"%PDF-1.7\nfixture " + name.encode())
            (folder / "ignored.txt").write_text("not a PDF")
            (folder / "nested").mkdir()
            (folder / "nested" / "ignored.pdf").write_bytes(b"%PDF-1.7")
            database = MagicMock()
            database.close = AsyncMock()
            session = database.sessions.return_value.__aenter__.return_value
            session.scalar.side_effect = ["existing-id", None, None]
            service = MagicMock()
            service.ingest = AsyncMock(return_value="stored-id")
            output = {"total_pages": 1, "result": {"purchase_orders": []}}
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch("backend.store_pos.Database", return_value=database),
                patch("backend.store_pos.POService", return_value=service),
                patch(
                    "backend.store_pos.extract",
                    side_effect=[ExtractionError("bad PDF"), output],
                ) as extraction,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = await store_folder(
                    folder, Settings(database_url="postgresql+asyncpg://fixture/po")
                )
            self.assertEqual(result, 1)
            self.assertEqual(extraction.call_count, 2)
            self.assertEqual(extraction.call_args_list[0].args[0].pdf.name, "b.PDF")
            service.ingest.assert_awaited_once_with(folder / "c.pdf", "c.pdf", output)
            database.close.assert_awaited_once()
            self.assertIn("1 stored, 1 already stored, 1 failed", stdout.getvalue())
            self.assertIn("bad PDF", stderr.getvalue())
            self.assertTrue(
                all((folder / name).exists() for name in ("a.pdf", "b.PDF", "c.pdf"))
            )

    async def test_database_failure_stops_before_extraction_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.pdf").write_bytes(b"%PDF-1.7")
            database = MagicMock()
            database.close = AsyncMock()
            session = database.sessions.return_value.__aenter__.return_value
            session.execute.side_effect = RuntimeError("missing tables")
            with (
                patch("backend.store_pos.Database", return_value=database),
                patch("backend.store_pos.extract") as extraction,
                self.assertRaisesRegex(RuntimeError, "missing tables"),
            ):
                await store_folder(
                    folder, Settings(database_url="postgresql+asyncpg://fixture/po")
                )
            extraction.assert_not_called()
            database.close.assert_awaited_once()

    async def test_success_and_invalid_pdf_header(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "a.pdf"
            path.write_bytes(b"%PDF-1.7\nfixture")
            database = MagicMock()
            database.close = AsyncMock()
            session = database.sessions.return_value.__aenter__.return_value
            session.scalar.return_value = None
            service = MagicMock()
            service.ingest = AsyncMock(return_value="stored-id")
            with (
                patch("backend.store_pos.Database", return_value=database),
                patch("backend.store_pos.POService", return_value=service),
                patch("backend.store_pos.extract") as extraction,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                settings = Settings(database_url="postgresql+asyncpg://fixture/po")
                self.assertEqual(await store_folder(folder, settings), 0)
                self.assertEqual(
                    extraction.call_args.kwargs["max_pages"], settings.max_pages
                )
                self.assertEqual(extraction.call_args.args[0].num_ctx, settings.num_ctx)
                self.assertEqual(
                    extraction.call_args.args[0].num_predict, settings.num_predict
                )
                path.write_bytes(b"not a PDF")
                self.assertEqual(await store_folder(folder, settings), 1)
                self.assertEqual(extraction.call_count, 1)
                service.ingest.assert_awaited_once()
