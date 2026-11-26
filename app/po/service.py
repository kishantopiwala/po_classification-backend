"""Shared read services and transactional ingestion, independent of HTTP/MCP."""

import asyncio
import hashlib
import shutil
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import selectinload

from .models import Document, POLineItem, PurchaseOrder
from .schemas import ExtractedDocument, SearchFilters


def record(obj):
    # Preserve Decimal values as strings, including their fractional precision.
    return jsonable_encoder(
        {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs},
        custom_encoder={Decimal: str},
    )


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def po_record(po):
    result = record(po)
    result["source_url"] = f"/api/v1/documents/{po.document_id}/download"
    return result


def contains(column, value):
    return column.icontains(value, autoescape=True)


def conditions(filters, *, items=False):
    f = filters
    po = PurchaseOrder
    clauses = []
    for key in ("po_number", "po_type", "currency"):
        if (value := getattr(f, key)) is not None:
            clauses.append(getattr(po, key) == value)
    for key in ("customer_name", "vendor_name"):
        if (value := getattr(f, key)) is not None:
            clauses.append(contains(getattr(po, key), value))
    date_column = getattr(po, f.date_field)
    for value, expression in (
        (f.date_from, lambda v: date_column >= v),
        (f.date_before, lambda v: date_column < v),
        (f.total_min, lambda v: po.total_amount >= v),
        (f.total_greater_than, lambda v: po.total_amount > v),
        (f.total_max, lambda v: po.total_amount <= v),
    ):
        if value is not None:
            clauses.append(expression(value))
    # Revised documents are retained but excluded until reviewed, preventing
    # accidental aggregation of both the original and its unreviewed revision.
    if not f.include_revisions:
        clauses.append(po.is_revised.is_(False))
    item_clauses = []
    if f.item_number is not None:
        item_clauses.append(POLineItem.item_number == f.item_number)
    if f.item_query is not None:
        item_clauses.append(contains(POLineItem.description, f.item_query))
    if f.unit_price_min is not None:
        item_clauses.append(POLineItem.unit_cost >= f.unit_price_min)
    if f.unit_price_max is not None:
        item_clauses.append(POLineItem.unit_cost <= f.unit_price_max)
    if items:
        clauses.extend(item_clauses)
    elif item_clauses:
        clauses.append(
            select(POLineItem.id)
            .where(POLineItem.po_id == po.id, *item_clauses)
            .exists()
        )
    return clauses


class POService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.active_chats: set[UUID] = set()

    async def ingest(self, path, filename, output):
        parsed = ExtractedDocument.model_validate(output["result"])
        digest = await asyncio.to_thread(file_hash, path)
        root = self.settings.document_storage_dir
        key = f"{uuid4()}.pdf"
        target = root / key
        copied = False
        try:
            async with self.database.sessions() as session, session.begin():
                # Serialize identical uploads across processes, not just one worker.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": int(digest[:15], 16)},
                )
                existing = await session.scalar(
                    select(Document).where(Document.sha256 == digest)
                )
                if existing:
                    return str(existing.id)
                await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
                copied = True
                await asyncio.to_thread(shutil.copyfile, path, target)
                from ..extraction.prompts import SYSTEM_PROMPT

                doc = Document(
                    filename=filename,
                    storage_key=key,
                    sha256=digest,
                    total_pages=output["total_pages"],
                    extraction_model=self.settings.model,
                    prompt_version=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                    raw_result=output["result"],
                )
                session.add(doc)
                await session.flush()
                for extracted in parsed.purchase_orders:
                    if not extracted.is_purchase_order:
                        continue
                    fields = extracted.model_dump(
                        exclude={"line_items", "is_purchase_order"}
                    )
                    po = PurchaseOrder(
                        document_id=doc.id,
                        **fields,
                        review_status="needs_review"
                        if extracted.is_revised or not extracted.po_number
                        else "unreviewed",
                    )
                    po.items = [
                        POLineItem(line_number=i, **item.model_dump())
                        for i, item in enumerate(extracted.line_items, 1)
                    ]
                    session.add(po)
                await session.flush()
                document_id = str(doc.id)
            return document_id
        except BaseException:
            if copied:
                await asyncio.to_thread(target.unlink, missing_ok=True)
            raise

    def bounded(self, filters):
        limit = (
            filters.limit
            if "limit" in filters.model_fields_set
            else self.settings.search_default_limit
        )
        return filters.model_copy(
            update={"limit": min(limit, self.settings.search_max_limit)}
        )

    async def search(self, filters: SearchFilters, *, items=False):
        f = self.bounded(filters)
        where = conditions(f, items=items)
        if items:
            base = (
                select(POLineItem, PurchaseOrder)
                .join(PurchaseOrder, POLineItem.po_id == PurchaseOrder.id)
                .where(*where)
            )
        else:
            base = select(PurchaseOrder).where(*where)
        order = {
            "issue_date_desc": PurchaseOrder.issue_date.desc().nulls_last(),
            "issue_date_asc": PurchaseOrder.issue_date.asc().nulls_last(),
            "total_desc": PurchaseOrder.total_amount.desc().nulls_last(),
        }[f.sort]
        async with self.database.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(base.subquery())
            )
            rows = await session.execute(
                base.order_by(
                    order,
                    PurchaseOrder.id,
                    *([POLineItem.line_number] if items else []),
                )
                .offset(f.offset)
                .limit(f.limit)
            )
            results = (
                [{**record(item), "purchase_order": po_record(po)} for item, po in rows]
                if items
                else [po_record(po) for po in rows.scalars()]
            )
        return {
            "results": results,
            "total": total,
            "limit": f.limit,
            "offset": f.offset,
            "next_offset": f.offset + f.limit if f.offset + f.limit < total else None,
            "revision_policy": "Includes revisions"
            if f.include_revisions
            else "Unreviewed revisions excluded; original versions may be outdated",
        }

    async def detail(self, po_id: UUID):
        async with self.database.sessions() as session:
            po = await session.scalar(
                select(PurchaseOrder)
                .options(selectinload(PurchaseOrder.items))
                .where(PurchaseOrder.id == po_id)
            )
            if po is None:
                return {"error": "not_found"}
            return {**po_record(po), "line_items": [record(item) for item in po.items]}

    async def summarize(self, filters: SearchFilters):
        query = (
            select(
                PurchaseOrder.currency,
                func.count(),
                func.sum(PurchaseOrder.total_amount),
                func.count(PurchaseOrder.total_amount),
            )
            .where(*conditions(filters))
            .group_by(PurchaseOrder.currency)
            .order_by(PurchaseOrder.currency)
        )
        async with self.database.sessions() as session:
            rows = await session.execute(query)
            return {
                "groups": [
                    {
                        "currency": currency,
                        "po_count": count,
                        "total_amount": str(total) if total is not None else None,
                        "missing_total_count": count - priced,
                    }
                    for currency, count, total, priced in rows
                ],
                "revision_policy": "Includes revisions"
                if filters.include_revisions
                else "Unreviewed revisions excluded; original versions may be outdated",
            }
