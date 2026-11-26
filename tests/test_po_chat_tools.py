"""Price-filter boundaries and recovery from invalid model tool arguments."""

import json
import sqlite3
import unittest
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from backend.app.core.config import Settings
from backend.app.mcp_server import create_mcp
from backend.app.po.chat import run_chat
from backend.app.po.models import PurchaseOrder
from backend.app.po.schemas import SearchFilters
from backend.app.po.service import conditions
from sqlalchemy import select
from sqlalchemy.dialects import sqlite


class PriceFilterTests(unittest.TestCase):
    def test_greater_than_excludes_equal_and_other_currencies(self):
        filters = SearchFilters.model_validate(
            {"total_greater_than": "1000", "currency": "USD"}
        )
        query = select(PurchaseOrder.po_number).where(*conditions(filters))
        compiled = query.compile(dialect=sqlite.dialect(paramstyle="named"))
        parameters = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in compiled.params.items()
        }
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute(
                "CREATE TABLE purchase_orders (po_number TEXT, total_amount NUMERIC, currency TEXT, is_revised BOOLEAN)"
            )
            database.executemany(
                "INSERT INTO purchase_orders VALUES (?, ?, ?, ?)",
                [
                    ("below", "999.99", "USD", False),
                    ("equal", "1000", "USD", False),
                    ("above", "1000.01", "USD", False),
                    ("other-currency", "2000", "CAD", False),
                    ("revised", "2000", "USD", True),
                ],
            )
            self.assertEqual(
                database.execute(str(compiled), parameters).fetchall(), [("above",)]
            )
        with self.assertRaises(ValueError):
            SearchFilters.model_validate(
                {"total_greater_than": "1000", "total_max": "1000"}
            )


class POChatToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_arguments_are_corrected_before_tool_execution(self):
        await self.check_tool_correction(
            {"invented_price_filter": 1000},
            {"total_greater_than": 1000, "currency": "USD"},
        )

    async def test_revision_output_field_is_corrected_to_boolean_input(self):
        await self.check_tool_correction(
            {"total_greater_than": 1000, "revision_policy": "include revised"},
            {"total_greater_than": 1000, "include_revisions": True},
        )

    async def check_tool_correction(self, invalid_filters, corrected_filters):
        tools = await create_mcp(Settings(database_url="")).list_tools()
        mcp = AsyncMock()
        mcp.__aenter__.return_value = mcp
        mcp.list_tools.return_value = SimpleNamespace(tools=tools)
        mcp.call_tool.return_value = SimpleNamespace(
            isError=False, structuredContent={"results": [], "total": 0}
        )
        transport = MagicMock()
        transport.__aenter__ = AsyncMock(return_value=(None, None, None))
        transport.__aexit__ = AsyncMock(return_value=False)
        client = AsyncMock()
        client.__aenter__.return_value = client

        def response(payload):
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request("POST", "http://ollama/api/chat"),
            )

        def tool_response(filters):
            return response(
                {
                    "done": True,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "search_purchase_orders",
                                    "arguments": {"filters": filters},
                                }
                            }
                        ],
                    },
                }
            )

        client.post.side_effect = [
            response({"capabilities": ["tools"]}),
            tool_response(invalid_filters),
            tool_response(corrected_filters),
            response(
                {
                    "done": True,
                    "message": {
                        "role": "assistant",
                        "content": "No matching purchase orders found.",
                    },
                }
            ),
        ]
        messages = [{"role": "user", "content": "List POs above USD 1000"}]
        with (
            patch("backend.app.po.chat.streamable_http_client", return_value=transport),
            patch("backend.app.po.chat.ClientSession", return_value=mcp),
            patch("backend.app.po.chat.httpx.AsyncClient", return_value=client),
        ):
            answer, results = await run_chat(Settings(database_url=""), messages)
        self.assertEqual(answer, "No matching purchase orders found.")
        mcp.call_tool.assert_awaited_once_with(
            "search_purchase_orders",
            {"filters": corrected_filters},
        )
        self.assertEqual(len(results), 1)
        feedback = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        ]
        self.assertEqual(feedback[0]["error"], "invalid_arguments")
        self.assertIn("include_revisions", feedback[0]["allowed_fields"])
        self.assertNotIn("revision_policy", feedback[0]["allowed_fields"])
        self.assertEqual(
            feedback[0]["expected_schema"]["properties"]["include_revisions"]["type"],
            "boolean",
        )
        for call in client.post.call_args_list[1:]:
            self.assertEqual(call.kwargs["json"]["options"]["num_ctx"], 16384)
            self.assertEqual(call.kwargs["json"]["options"]["num_predict"], 8192)
