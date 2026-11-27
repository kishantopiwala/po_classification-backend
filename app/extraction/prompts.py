"""Load fine-tuned extraction instructions and supply per-document context."""

import json
from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parents[3] / "extraction_prompt_finetuned.txt"
SYSTEM_PROMPT = PROMPT_FILE.read_text(encoding="utf-8").strip()
if not SYSTEM_PROMPT:
    raise ValueError(f"Extraction prompt file is empty: {PROMPT_FILE}")


def build_prompt(document_name: str, total_pages: int) -> str:
    """Supply PDF context; extraction rules and output format are in SYSTEM_PROMPT."""
    return f"""File: {json.dumps(document_name)}
All {total_pages} pages are attached in document order.
Analyze this PDF using the system instructions and return the specified JSON
object with has_artwork_proof, artwork_po_number, artwork_id, and purchase_orders.
Use the exact filename above for po_filename. If no Purchase Order is found,
return an empty purchase_orders array.
Check primary PO and Customer PO fields separately across all pages, preserving
Customer PO leading zeros even on vendor POs.
Base the result only on this PDF; use null for unavailable values and do not
infer documents or artwork proofs that are not visible in the supplied pages.
"""
