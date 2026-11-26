from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .models import Document
from .schemas import SearchFilters
from .service import POService

router = APIRouter(tags=["purchase orders"])


def service(request: Request):
    value = getattr(request.app.state, "po_service", None)
    if value is None:
        raise HTTPException(503, "PO database is not configured")
    return value


POServiceDependency = Annotated[POService, Depends(service)]


@router.get("/purchase-orders")
async def list_orders(
    request: Request,
    filters: Annotated[SearchFilters, Query()],
    po: POServiceDependency,
):
    if "limit" not in request.query_params:
        filters = filters.model_copy(update={"limit": po.settings.search_default_limit})
    return await po.search(filters)


@router.post("/purchase-orders/search")
async def search_orders(filters: SearchFilters, po: POServiceDependency):
    return await po.search(filters)


@router.get("/purchase-orders/{po_id}")
async def get_order(po_id: UUID, po: POServiceDependency):
    result = await po.detail(po_id)
    if result.get("error"):
        raise HTTPException(404, "PO not found")
    return result


@router.get("/documents/{document_id}/download")
async def download(document_id: UUID, po: POServiceDependency):
    async with po.database.sessions() as session:
        document = await session.get(Document, document_id)
        if document is None:
            raise HTTPException(404, "Document not found")
        root = po.settings.document_storage_dir.resolve()
        path = (root / document.storage_key).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "Document file not found")
        return FileResponse(
            path, filename=document.filename, media_type="application/pdf"
        )
