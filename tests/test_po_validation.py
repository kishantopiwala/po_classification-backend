"""Configuration and input validation checks that do not need PostgreSQL."""

import unittest
from dataclasses import replace

from backend.app.core.config import Settings
from backend.app.database import Database
from backend.app.main import create_app
from backend.app.po.schemas import ExtractedItem, SearchFilters
from fastapi.testclient import TestClient
from pydantic import ValidationError


class POValidationTests(unittest.TestCase):
    def test_filters_reject_unsupported_fields_and_invalid_ranges(self):
        for body in (
            {"sql": "SELECT 1"},
            {"limit": 101},
            {"offset": -1},
            {"date_from": "2024-05-01", "date_before": "2024-04-01"},
            {"date_field": "upload_date"},
            {"unit_price_min": "20", "unit_price_max": "10"},
            {"total_min": "NaN"},
        ):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                SearchFilters(**body)

    def test_decimal_validation_and_original_units(self):
        item = ExtractedItem(qty="100.0000", unit_of_measure="M", unit_cost="37.900001")
        self.assertEqual(str(item.qty), "100.0000")
        self.assertEqual(str(item.unit_cost), "37.900001")
        for value in ("NaN", "Infinity", "1000000000000000000.000001"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ExtractedItem(unit_cost=value)

    def test_async_driver_and_search_limit_configuration(self):
        with self.assertRaises(ValueError):
            Database("postgresql://localhost/po")
        for changes in (
            {"search_max_limit": 101},
            {"search_default_limit": 0},
            {"search_default_limit": 21, "search_max_limit": 20},
            {"mcp_port": 0},
            {"mcp_path": "mcp"},
            {"chat_timeout": 0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(Settings(database_url=""), **changes)

    def test_po_endpoints_report_missing_database(self):
        with TestClient(create_app(Settings(database_url=""))) as client:
            self.assertEqual(client.get("/api/v1/purchase-orders").status_code, 503)
            self.assertEqual(
                client.post(
                    "/api/v1/po-chat", json={"message": "Find POs"}
                ).status_code,
                503,
            )
