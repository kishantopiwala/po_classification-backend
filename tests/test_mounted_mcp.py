"""Exercise API and MCP on the same ASGI app without an external database."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from backend.app.core.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.po.service import POService
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class MountedMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_and_tools_share_service_and_lifecycle(self):
        database = AsyncMock(spec=Database)
        service = AsyncMock(spec=POService)
        service.search.return_value = {"results": [], "total": 0}
        service.detail.return_value = {"error": "not_found"}
        service.summarize.return_value = {"groups": []}
        settings = Settings(database_url="postgresql+asyncpg://fixture/po")
        with (
            patch("backend.app.main.Database", return_value=database),
            patch("backend.app.main.POService", return_value=service),
        ):
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                self.assertIs(app.state.po_service, service)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost:8000",
                ) as client:
                    self.assertEqual((await client.get("/health")).status_code, 200)
                    self.assertEqual((await client.get("/docs")).status_code, 200)
                    async with (
                        streamable_http_client(
                            "http://localhost:8000/mcp", http_client=client
                        ) as (read, write, _),
                        ClientSession(read, write) as session,
                    ):
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        self.assertEqual(len(tools), 4)
                        for name, arguments in (
                            ("search_purchase_orders", {"filters": {}}),
                            ("search_po_items", {"filters": {}}),
                            (
                                "get_purchase_order",
                                {"po_id": "00000000-0000-0000-0000-000000000001"},
                            ),
                            ("summarize_purchase_orders", {"filters": {}}),
                        ):
                            result = await session.call_tool(name, arguments)
                            self.assertFalse(result.isError, result)
                            self.assertIsNotNone(result.structuredContent)
                database.close.assert_not_awaited()
            database.close.assert_awaited_once()
            self.assertEqual(service.search.await_count, 2)
            service.detail.assert_awaited_once()
            service.summarize.assert_awaited_once()

    async def test_custom_mcp_path_without_database(self):
        app = create_app(Settings(database_url="", mcp_path="/tools/mcp"))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client,
        ):
            self.assertEqual((await client.get("/health")).status_code, 200)
            async with (
                streamable_http_client(
                    "http://localhost:8000/tools/mcp", http_client=client
                ) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                self.assertEqual(len((await session.list_tools()).tools), 4)
                result = await session.call_tool(
                    "search_purchase_orders", {"filters": {}}
                )
                self.assertTrue(result.isError)
                self.assertIn("DATABASE_URL", result.content[0].text)
