"""Backend entry point for fastapi run; loads backend/.env before app creation."""

from pathlib import Path

from dotenv import load_dotenv

__all__ = ["app"]

load_dotenv(Path(__file__).resolve().parent / ".env")

if __package__:
    from .app.main import app
else:
    from app.main import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, env_file=".env")
