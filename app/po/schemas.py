from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(max_digits=24, decimal_places=6, allow_inf_nan=False)]


class ExtractedItem(BaseModel):
    item_number: str | None = Field(default=None, max_length=255)
    description: str | None = None
    qty: Money | None = None
    unit_of_measure: str | None = Field(default=None, max_length=100)
    unit_cost: Money | None = None
    price_basis: str | None = Field(default=None, max_length=255)
    amount: Money | None = None


class ExtractedPO(BaseModel):
    is_purchase_order: bool = True
    po_number: str | None = Field(default=None, max_length=255)
    po_type: Literal["customer", "vendor"] | None = None
    customer_po: str | None = Field(default=None, max_length=255)
    customer_po_reference: str | None = Field(default=None, max_length=255)
    customer_name: str | None = None
    vendor_name: str | None = None
    vendor_email: str | None = None
    issue_date: date | None = None
    estimated_ship_date: date | None = None
    total_amount: Money | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    ship_to_address: str | None = None
    is_revised: bool = False
    extraction_notes: str | None = None
    line_items: list[ExtractedItem] = Field(default_factory=list)


class ExtractedDocument(BaseModel):
    purchase_orders: list[ExtractedPO]


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    po_number: str | None = Field(default=None, min_length=1, max_length=255)
    po_type: Literal["customer", "vendor"] | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=255)
    vendor_name: str | None = Field(default=None, min_length=1, max_length=255)
    date_field: Literal["issue_date", "estimated_ship_date"] = "issue_date"
    date_from: date | None = None
    date_before: date | None = None
    item_number: str | None = Field(default=None, min_length=1, max_length=255)
    item_query: str | None = Field(default=None, min_length=1, max_length=255)
    unit_price_min: Decimal | None = None
    unit_price_max: Decimal | None = None
    total_min: Decimal | None = Field(
        default=None,
        description="PO total greater than or equal to this amount (inclusive).",
    )
    total_greater_than: Decimal | None = Field(
        default=None,
        description="PO total strictly greater than this amount; excludes equal totals.",
    )
    total_max: Decimal | None = Field(
        default=None,
        description="PO total less than or equal to this amount (inclusive).",
    )
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    include_revisions: bool = Field(
        default=False,
        description="Set true to include revised POs, false to exclude them. This is the revision INPUT filter; revision_policy is output-only and must not be sent.",
    )
    sort: Literal["issue_date_desc", "issue_date_asc", "total_desc"] = "issue_date_desc"
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)

    @model_validator(mode="after")
    def ranges(self):
        for lower, upper in (
            ("date_from", "date_before"),
            ("unit_price_min", "unit_price_max"),
            ("total_min", "total_max"),
            ("total_greater_than", "total_max"),
        ):
            lo, hi = getattr(self, lower), getattr(self, upper)
            if (
                lo is not None
                and hi is not None
                and (
                    lo >= hi
                    if lower in {"date_from", "total_greater_than"}
                    else lo > hi
                )
            ):
                raise ValueError(f"Invalid range: {lower}, {upper}")
        return self
