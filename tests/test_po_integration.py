"""PostgreSQL + real MCP HTTP + Ollama fixture integration tests.

Set TEST_DATABASE_URL to a migrated, dedicated PostgreSQL database. Tests only
remove records they create. No installed Ollama model is required.
"""

import asyncio
import os
import socket
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from backend.app.core.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.mcp_server import create_mcp
from backend.app.po.models import (
    ChatMessage,
    ChatSession,
    Document,
    POLineItem,
    PurchaseOrder,
)
from backend.app.po.schemas import SearchFilters
from backend.app.po.service import POService
from fastapi import FastAPI, Request
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

URL = os.environ.get("TEST_DATABASE_URL", "")


@asynccontextmanager
async def serve(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Fixture server exited")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        listener.close()


@unittest.skipUnless(URL, "Set TEST_DATABASE_URL to run PostgreSQL integration tests")
class POIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.marker = str(uuid4())
        self.settings = Settings(
            database_url=URL,
            document_storage_dir=self.root / "documents",
            upload_dir=self.root / "uploads",
            chat_model="fixture-tools",
            chat_timeout=10,
        )
        self.db = Database(URL)
        self.po = POService(self.db, self.settings)
        self.doc_ids = []
        self.session_ids = []

    async def asyncTearDown(self):
        async with self.db.sessions() as session, session.begin():
            if self.doc_ids:
                ids = select(PurchaseOrder.id).where(
                    PurchaseOrder.document_id.in_(self.doc_ids)
                )
                await session.execute(
                    delete(POLineItem).where(POLineItem.po_id.in_(ids))
                )
                await session.execute(
                    delete(PurchaseOrder).where(
                        PurchaseOrder.document_id.in_(self.doc_ids)
                    )
                )
                await session.execute(
                    delete(Document).where(Document.id.in_(self.doc_ids))
                )
            if self.session_ids:
                await session.execute(
                    delete(ChatMessage).where(
                        ChatMessage.session_id.in_(self.session_ids)
                    )
                )
                await session.execute(
                    delete(ChatSession).where(ChatSession.id.in_(self.session_ids))
                )
        await self.db.close()
        self.temp.cleanup()

    def output(self, **overrides):
        order = {
            "po_number": "0001983",
            "po_type": "customer",
            "customer_name": self.marker,
            "vendor_name": "DPI",
            "issue_date": "2024-04-24",
            "estimated_ship_date": "2024-06-04",
            "currency": "USD",
            "total_amount": "94.750001",
            "line_items": [
                {
                    "item_number": "00123",
                    "description": "Honeycrisp bags 100%",
                    "qty": "2.5",
                    "unit_of_measure": "M",
                    "unit_cost": "37.900000",
                    "price_basis": "per thousand",
                    "amount": "94.750001",
                }
            ],
        }
        order.update(overrides)
        return {"total_pages": 1, "result": {"purchase_orders": [order]}}

    async def ingest(self, **overrides):
        source = self.root / f"{uuid4()}.pdf"
        source.write_bytes(b"%PDF-1.4 " + str(uuid4()).encode())
        output = self.output(**overrides)
        doc = UUID(await self.po.ingest(source, "purchase.pdf", output))
        self.doc_ids.append(doc)
        return source, output, doc

    async def test_persistence_duplicate_precision_search_and_aggregation(self):
        source, output, doc = await self.ingest()
        same = await self.po.ingest(source, "duplicate.pdf", output)
        self.assertEqual(same, str(doc))
        await self.ingest(po_number="OTHER", currency="EUR", total_amount="10")
        await self.ingest(po_number="REVISED", is_revised=True, total_amount="200")
        f = SearchFilters(
            customer_name=self.marker,
            item_query="Honeycrisp",
            date_from="2024-04-01",
            date_before="2024-05-01",
            limit=1,
        )
        page = await self.po.search(f)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["results"]), 1)
        self.assertEqual(page["next_offset"], 1)
        detail = await self.po.detail(UUID(page["results"][0]["id"]))
        self.assertEqual(detail["line_items"][0]["unit_cost"], "37.900000")
        self.assertEqual(detail["line_items"][0]["unit_of_measure"], "M")
        totals = await self.po.summarize(f)
        self.assertEqual(
            {g["currency"]: g["total_amount"] for g in totals["groups"]},
            {"USD": "94.750001", "EUR": "10.000000"},
        )
        items = await self.po.search(
            SearchFilters(
                customer_name=self.marker,
                item_number="00123",
                currency="USD",
                unit_price_max="38",
            ),
            items=True,
        )
        self.assertEqual(items["total"], 1)
        self.assertEqual(items["results"][0]["purchase_order"]["po_number"], "0001983")
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(customer_name=self.marker, item_query="100%")
                )
            )["total"],
            2,
        )
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(customer_name=self.marker, item_query="%")
                )
            )["total"],
            2,
        )
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(customer_name=self.marker, po_number="' OR 1=1 --")
                )
            )["total"],
            0,
        )
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(
                        customer_name=self.marker,
                        date_from="2024-04-24",
                        date_before="2024-04-25",
                    )
                )
            )["total"],
            2,
        )
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(customer_name=self.marker, date_before="2024-04-24")
                )
            )["total"],
            0,
        )
        self.assertEqual(
            (
                await self.po.search(
                    SearchFilters(customer_name=self.marker, include_revisions=True)
                )
            )["total"],
            3,
        )
        # Independent engine verifies persistence beyond the ingesting service.
        fresh = Database(URL)
        try:
            self.assertEqual(
                (
                    await POService(fresh, self.settings).search(
                        SearchFilters(customer_name=self.marker)
                    )
                )["total"],
                2,
            )
        finally:
            await fresh.close()

    async def test_validation_rolls_back_without_files(self):
        source = self.root / "invalid.pdf"
        source.write_bytes(b"%PDF-1.4 invalid")
        with self.assertRaises(ValueError):
            await self.po.ingest(
                source, "invalid.pdf", self.output(issue_date="not-a-date")
            )
        self.assertFalse(self.settings.document_storage_dir.exists())
        # DB error after copy must roll back rows and remove the copied PDF.
        with self.assertRaises(DBAPIError):
            await self.po.ingest(source, "x" * 256, self.output())
        self.assertEqual(list(self.settings.document_storage_dir.iterdir()), [])
        self.assertEqual(
            (await self.po.search(SearchFilters(customer_name=self.marker)))["total"], 0
        )

    async def test_concurrent_duplicate_uploads(self):
        source = self.root / "concurrent.pdf"
        source.write_bytes(b"%PDF-1.4 " + self.marker.encode())
        output = self.output()
        ids = await asyncio.gather(
            *(self.po.ingest(source, "again.pdf", output) for _ in range(3))
        )
        self.doc_ids.append(UUID(ids[0]))
        self.assertEqual(ids, [ids[0]] * 3)
        self.assertEqual(len(list(self.settings.document_storage_dir.iterdir())), 1)

    async def test_500_orders_search_paginates_and_aggregates_all_matches(self):
        source = self.root / "batch.pdf"
        source.write_bytes(b"%PDF-1.4 " + self.marker.encode())
        orders = [
            self.output(po_number=f"PO-{index:04d}", total_amount="1.000001")["result"][
                "purchase_orders"
            ][0]
            for index in range(500)
        ]
        doc = await self.po.ingest(
            source,
            "batch.pdf",
            {"total_pages": 1, "result": {"purchase_orders": orders}},
        )
        self.doc_ids.append(UUID(doc))
        page = await self.po.search(SearchFilters(customer_name=self.marker, limit=20))
        self.assertEqual(page["total"], 500)
        self.assertEqual(len(page["results"]), 20)
        self.assertEqual(page["next_offset"], 20)
        exact = await self.po.search(
            SearchFilters(customer_name=self.marker, po_number="PO-0499")
        )
        self.assertEqual(exact["total"], 1)
        summary = await self.po.summarize(
            SearchFilters(customer_name=self.marker, limit=1)
        )
        self.assertEqual(summary["groups"][0]["po_count"], 500)
        self.assertEqual(summary["groups"][0]["total_amount"], "500.000500")
        app = create_app(
            replace(self.settings, search_default_limit=7, search_max_limit=10)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.get(
                "/api/v1/purchase-orders", params={"customer_name": self.marker}
            )
            self.assertEqual(response.json()["limit"], 7)
            self.assertEqual(len(response.json()["results"]), 7)
            response = await client.post(
                "/api/v1/purchase-orders/search",
                json={"customer_name": self.marker, "limit": 50},
            )
            self.assertEqual(response.json()["limit"], 10)

    async def test_rest_upload_download_and_restart(self):
        output = self.output()

        def extractor(*args, **kwargs):
            return output

        app = create_app(self.settings, extractor)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                "/api/v1/extractions",
                files={
                    "file": (
                        "po.pdf",
                        b"%PDF-1.4 " + self.marker.encode(),
                        "application/pdf",
                    )
                },
            )
            self.assertEqual(response.status_code, 202)
            body = response.json()
            async with asyncio.timeout(5):
                while True:
                    snapshot = (await client.get(body["status_url"])).json()
                    if snapshot["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(snapshot["status"], "completed", snapshot)
            self.doc_ids.append(UUID(snapshot["document_id"]))
            rows = (
                await client.get(
                    "/api/v1/purchase-orders", params={"customer_name": self.marker}
                )
            ).json()
            self.assertEqual(rows["total"], 1)
            detail = await client.get(
                "/api/v1/purchase-orders/" + rows["results"][0]["id"]
            )
            self.assertEqual(detail.status_code, 200)
            pdf = await client.get(rows["results"][0]["source_url"])
            self.assertTrue(pdf.content.startswith(b"%PDF-"))
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/purchase-orders/search", json={"limit": 1000}
                    )
                ).status_code,
                422,
            )
        restarted = create_app(self.settings, extractor)
        async with (
            restarted.router.lifespan_context(restarted),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://test"
            ) as client,
        ):
            self.assertEqual(
                (
                    await client.get(
                        "/api/v1/purchase-orders",
                        params={"customer_name": self.marker},
                    )
                ).json()["total"],
                1,
            )

    async def test_real_mcp_discovery_calls_and_validation(self):
        await self.ingest()
        server = create_mcp(self.settings, self.po)
        async with (
            serve(server.streamable_http_app()) as url,
            streamable_http_client(url + "/mcp") as (read, write, _),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            tools = (await client.list_tools()).tools
            self.assertEqual(len(tools), 4)
            result = await client.call_tool(
                "search_po_items",
                {
                    "filters": {
                        "customer_name": self.marker,
                        "item_query": "Honeycrisp",
                    }
                },
            )
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["total"], 1)
            bad = await client.call_tool(
                "search_purchase_orders", {"filters": {"limit": 10000}}
            )
            self.assertTrue(bad.isError)
            unknown = await client.call_tool(
                "execute_sql", {"sql": "DROP TABLE documents"}
            )
            self.assertTrue(unknown.isError)

    async def test_chat_tool_loop_context_and_limit(self):
        await self.ingest()
        fixture = FastAPI()
        calls = []
        mode = {"loop": False}

        @fixture.post("/api/show")
        async def show():
            return {"capabilities": ["tools"]}

        @fixture.post("/api/chat")
        async def chat(request: Request):
            body = await request.json()
            calls.append(body)
            if body["messages"][-1]["role"] == "tool" and not mode["loop"]:
                return {
                    "done": True,
                    "message": {
                        "role": "assistant",
                        "content": "Found PO 0001983 with Honeycrisp bags.",
                    },
                }
            return {
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_po_items",
                                "arguments": {
                                    "filters": {"customer_name": self.marker}
                                },
                            }
                        }
                    ],
                },
            }

        async with (
            serve(create_mcp(self.settings, self.po).streamable_http_app()) as mcp_url,
            serve(fixture) as ollama_url,
        ):
            settings = replace(
                self.settings,
                mcp_server_url=mcp_url + "/mcp",
                host=ollama_url,
                max_tool_calls=2,
            )
            app = create_app(settings)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                response = await client.post(
                    "/api/v1/po-chat", json={"message": "Find Honeycrisp"}
                )
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                sid = UUID(result["session_id"])
                self.session_ids.append(sid)
                self.assertEqual(result["tool_results"][0]["data"]["total"], 1)
                self.assertEqual(len(calls[0]["tools"]), 4)
                followup = await client.post(
                    "/api/v1/po-chat",
                    json={"session_id": str(sid), "message": "Show its price"},
                )
                self.assertEqual(followup.status_code, 200, followup.text)
                self.assertTrue(
                    any("0001983" in m.get("content", "") for m in calls[2]["messages"])
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/api/v1/po-chat",
                            json={"session_id": str(uuid4()), "message": "Hi"},
                        )
                    ).status_code,
                    404,
                )
                mode["loop"] = True
                failure = await client.post(
                    "/api/v1/po-chat",
                    json={"session_id": str(sid), "message": "Loop"},
                )
                self.assertEqual(failure.status_code, 502)
                async with self.db.sessions() as session:
                    messages = (
                        await session.scalars(
                            select(ChatMessage)
                            .where(ChatMessage.session_id == sid)
                            .order_by(ChatMessage.sequence)
                        )
                    ).all()
                    self.assertEqual(
                        [m.role for m in messages],
                        ["user", "assistant", "user", "assistant"],
                    )
