"""Async migrations. DATABASE_URL is supplied by the environment or backend/.env."""

import asyncio
import os
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from app.po.models import Base

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
target_metadata = Base.metadata
url = os.environ.get("DATABASE_URL", "")
if not url.startswith("postgresql+asyncpg://"):
    raise ValueError("Set DATABASE_URL to a postgresql+asyncpg:// URL")


def migrate(connection):
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
