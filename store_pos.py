"""Extract PDFs from po_to_store and persist their POs using backend/.env."""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

if __package__:
    from .app.core.config import BACKEND_DIR, Settings
    from .app.database import Database
    from .app.extraction.service import ExtractionError, extract
    from .app.po.models import Document
    from .app.po.service import POService, file_hash
else:
    from app.core.config import BACKEND_DIR, Settings
    from app.database import Database
    from app.extraction.service import ExtractionError, extract
    from app.po.models import Document
    from app.po.service import POService, file_hash


async def store_folder(folder: Path, settings: Settings) -> int:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    files = sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: path.name.lower(),
    )
    if not files:
        print(f"No PDF files found in {folder}")
        return 0
    if not settings.database_url:
        raise ValueError("Set DATABASE_URL in backend/.env before storing POs")

    database = Database(settings.database_url)
    service = POService(database, settings)
    stored = skipped = failed = 0
    try:
        # Fail before inference if the database is unavailable or unmigrated.
        async with database.sessions() as session:
            await session.execute(select(Document.id).limit(0))

        for index, path in enumerate(files, 1):
            print(f"[{index}/{len(files)}] {path.name}", flush=True)
            try:
                if len(path.name) > 255:
                    raise ExtractionError("PDF filename must be at most 255 characters")
                if path.stat().st_size > settings.max_upload_bytes:
                    raise ExtractionError("PDF exceeds MAX_UPLOAD_BYTES")
                with path.open("rb") as source:
                    if b"%PDF-" not in source.read(1024):
                        raise ExtractionError("File does not contain a PDF header")
                digest = file_hash(path)
                async with database.sessions() as session:
                    existing_id = await session.scalar(
                        select(Document.id).where(Document.sha256 == digest)
                    )
                if existing_id is not None:
                    skipped += 1
                    print(f"  Already stored: document_id={existing_id}", flush=True)
                    continue

                options = argparse.Namespace(
                    pdf=path,
                    host=settings.host,
                    model=settings.model,
                    dpi=settings.dpi,
                    timeout=settings.timeout,
                    num_ctx=settings.num_ctx,
                    num_predict=settings.num_predict,
                )
                output = extract(
                    options, max_pages=settings.max_pages, document_name=path.name
                )
                document_id = await service.ingest(path, path.name, output)
                stored += 1
                print(f"  Stored: document_id={document_id}", flush=True)
            except (ExtractionError, OSError, ValueError, SQLAlchemyError) as exc:
                failed += 1
                print(f"  Failed {path.name}: {exc}", file=sys.stderr, flush=True)
    finally:
        await database.close()

    print(f"Finished: {stored} stored, {skipped} already stored, {failed} failed.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        type=Path,
        default=BACKEND_DIR / "po_to_store",
        help="PDF folder (default: backend/po_to_store; reads immediate files only)",
    )
    args = parser.parse_args(argv)
    load_dotenv(BACKEND_DIR / ".env")
    try:
        return asyncio.run(store_folder(args.folder, Settings()))
    except KeyboardInterrupt:
        print("Stopped. You can rerun the script to continue.", file=sys.stderr)
        return 130
    except (OSError, ValueError, SQLAlchemyError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        print(
            "Check DATABASE_URL/PostgreSQL and run `uv run alembic upgrade head` from backend/.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
