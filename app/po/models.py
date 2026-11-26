from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Identity as SQLIdentity,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Identity, Base):
    __tablename__ = "documents"
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    total_pages: Mapped[int] = mapped_column(Integer)
    extraction_model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(64))
    raw_result: Mapped[dict] = mapped_column(JSONB)


class PurchaseOrder(Identity, Base):
    __tablename__ = "purchase_orders"
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    po_number: Mapped[str | None] = mapped_column(String(255), index=True)
    po_type: Mapped[str | None] = mapped_column(String(20), index=True)
    customer_po: Mapped[str | None] = mapped_column(String(255))
    customer_po_reference: Mapped[str | None] = mapped_column(String(255))
    customer_name: Mapped[str | None] = mapped_column(Text, index=True)
    vendor_name: Mapped[str | None] = mapped_column(Text, index=True)
    vendor_email: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    estimated_ship_date: Mapped[date | None] = mapped_column(Date, index=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    ship_to_address: Mapped[str | None] = mapped_column(Text)
    is_revised: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(30), default="unreviewed")
    extraction_notes: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list[POLineItem]] = relationship(
        lazy="raise", cascade="all, delete-orphan", order_by="POLineItem.line_number"
    )


class POLineItem(Identity, Base):
    __tablename__ = "po_line_items"
    po_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer)
    item_number: Mapped[str | None] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    unit_of_measure: Mapped[str | None] = mapped_column(String(100))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    price_basis: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))


class ChatSession(Identity, Base):
    __tablename__ = "chat_sessions"


class ChatMessage(Identity, Base):
    __tablename__ = "chat_messages"
    sequence: Mapped[int] = mapped_column(BigInteger, SQLIdentity(), unique=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    tool_results: Mapped[list] = mapped_column(JSONB, default=list)
