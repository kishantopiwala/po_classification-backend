"""Validated environment settings; .env files are optional."""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def storage_path():
    path = Path(os.getenv("DOCUMENT_STORAGE_DIR", "./storage/documents")).expanduser()
    return path if path.is_absolute() else BACKEND_DIR / path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    document_storage_dir: Path = field(default_factory=storage_path)
    mcp_host: str = field(default_factory=lambda: os.getenv("MCP_HOST", "127.0.0.1"))
    mcp_port: int = field(default_factory=lambda: int(os.getenv("MCP_PORT", "8000")))
    mcp_path: str = field(default_factory=lambda: os.getenv("MCP_PATH", "/mcp"))
    mcp_server_url: str = field(
        default_factory=lambda: os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
    )
    mcp_timeout: float = field(
        default_factory=lambda: float(os.getenv("MCP_TIMEOUT_SECONDS", "30"))
    )
    chat_model: str = field(
        default_factory=lambda: os.getenv("PO_CHAT_MODEL", "gemma4:e4b")
    )
    chat_timeout: float = field(
        default_factory=lambda: float(os.getenv("PO_CHAT_TIMEOUT_SECONDS", "120"))
    )
    max_tool_calls: int = field(
        default_factory=lambda: int(os.getenv("PO_CHAT_MAX_TOOL_CALLS", "8"))
    )
    search_default_limit: int = field(
        default_factory=lambda: int(os.getenv("PO_SEARCH_DEFAULT_LIMIT", "20"))
    )
    search_max_limit: int = field(
        default_factory=lambda: int(os.getenv("PO_SEARCH_MAX_LIMIT", "100"))
    )
    host: str = field(
        default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434")
    )
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "gemma4:e4b"))
    dpi: int = field(default_factory=lambda: int(os.getenv("PDF_DPI", "150")))
    timeout: float = field(
        default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "600"))
    )
    num_ctx: int = field(
        default_factory=lambda: int(os.getenv("OLLAMA_NUM_CTX", "16384"))
    )
    num_predict: int = field(
        default_factory=lambda: int(os.getenv("OLLAMA_NUM_PREDICT", "8192"))
    )
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", "20971520"))
    )
    max_pages: int = field(
        default_factory=lambda: int(os.getenv("MAX_PDF_PAGES", "20"))
    )
    queue_size: int = field(
        default_factory=lambda: int(os.getenv("JOB_QUEUE_SIZE", "10"))
    )
    max_jobs: int = field(
        default_factory=lambda: int(os.getenv("MAX_RETAINED_JOBS", "100"))
    )
    retention: float = field(
        default_factory=lambda: float(os.getenv("JOB_RETENTION_SECONDS", "3600"))
    )
    heartbeat: float = field(
        default_factory=lambda: float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))
    )
    upload_dir: Path = field(
        default_factory=lambda: Path(os.getenv("UPLOAD_DIR", tempfile.gettempdir()))
    )

    def __post_init__(self):
        for name in (
            "dpi",
            "timeout",
            "num_ctx",
            "num_predict",
            "max_upload_bytes",
            "max_pages",
            "queue_size",
            "max_jobs",
            "retention",
            "heartbeat",
            "mcp_timeout",
            "chat_timeout",
            "max_tool_calls",
            "search_default_limit",
            "search_max_limit",
        ):
            value = getattr(self, name)
            if not 0 < value < float("inf"):
                raise ValueError(f"{name} must be finite and greater than zero")
        if not 1 <= self.search_default_limit <= self.search_max_limit <= 100:
            raise ValueError(
                "Search limits must satisfy 1 <= default <= maximum <= 100"
            )
        if self.num_predict >= self.num_ctx:
            raise ValueError("OLLAMA_NUM_PREDICT must be less than OLLAMA_NUM_CTX")
        if not 1 <= self.mcp_port <= 65535 or not self.mcp_path.startswith("/"):
            raise ValueError("Invalid MCP port or path")
