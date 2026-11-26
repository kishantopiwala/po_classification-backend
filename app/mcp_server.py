"""Read-only PO tools served over MCP Streamable HTTP (local deployment)."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from .core.config import Settings
from .database import Database
from .po.schemas import SearchFilters
from .po.service import POService


def create_mcp(
    settings: Settings | None = None,
    service: POService | None = None,
    *,
    service_provider: Callable[[], POService | None] | None = None,
) -> FastMCP[dict[str, Any]]:
    config = settings or Settings()
    active_service: POService | None = None

    def get_service() -> POService:
        if service_provider is not None:
            shared_service = service_provider()
            if shared_service is None:
                raise RuntimeError("DATABASE_URL is required for PO tools")
            return shared_service
        if service is not None:
            return service
        if active_service is None:
            raise RuntimeError("PO MCP server lifespan is not running")
        return active_service

    @asynccontextmanager
    async def lifespan(
        server: FastMCP[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal active_service
        database = None
        if service_provider is not None:
            pass  # The parent FastAPI lifespan owns the shared service/database.
        elif service is None:
            if not config.database_url:
                raise ValueError("DATABASE_URL is required for the PO MCP server")
            database = Database(config.database_url)
            active_service = POService(database, config)
        else:
            active_service = service
        try:
            yield {}
        finally:
            active_service = None
            if database:
                await database.close()

    server = FastMCP(
        "Purchase orders",
        host=config.mcp_host,
        port=config.mcp_port,
        streamable_http_path=config.mcp_path,
        stateless_http=True,
        json_response=True,
        lifespan=lifespan,
    )

    def result(data: dict[str, Any]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data))],
            structuredContent=data,
        )

    @server.tool()
    async def search_purchase_orders(filters: SearchFilters) -> CallToolResult:
        """Search PO headers by number, company, type, dates or matching items. Dates use inclusive date_from and exclusive date_before. To include revised POs, set filters.include_revisions=true (boolean). Never send revision_policy; it is output-only. Prices require currency context. Paginate with filters.offset/limit."""
        return result(await get_service().search(filters))

    @server.tool()
    async def get_purchase_order(po_id: UUID) -> CallToolResult:
        """Get all header fields, line items and PDF reference for an internal PO UUID returned by search. A printed PO number is not a UUID."""
        return result(await get_service().detail(po_id))

    @server.tool()
    async def search_po_items(filters: SearchFilters) -> CallToolResult:
        """Search individual line items by exact item number, description or unit price, with PO/date/company filters. Returned prices preserve printed units and price basis; do not assume per-piece prices."""
        return result(await get_service().search(filters, items=True))

    @server.tool()
    async def summarize_purchase_orders(filters: SearchFilters) -> CallToolResult:
        """Count all matching POs and sum their PO totals, grouped by currency. Pagination does not limit aggregation. Totals are PO totals, not matching-line subtotals. Revisions are excluded unless explicitly requested."""
        return result(await get_service().summarize(filters))

    return server


def main():
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    create_mcp().run(transport="streamable-http")


if __name__ == "__main__":
    main()
