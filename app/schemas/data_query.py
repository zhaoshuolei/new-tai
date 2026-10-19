"""Schemas and value units for querying current custom-data rows.

Money values (``revenue``, ``cost``, and ``gross_profit``) are RMB decimal
amounts. ``gross_margin`` is a ratio, not a percentage: gross profit divided by
revenue, and is ``None`` when revenue is zero.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

type CustomRecordValue = str | int | Decimal | date | bool


class DataFieldCondition(BaseModel):
    """One typed condition; the service validates type/operator compatibility."""

    model_config = ConfigDict(extra="forbid")

    field_id: int
    operator: str
    value: Any


class DataRecordSearch(BaseModel):
    """AND-only filters, stable sorting, and bounded pagination."""

    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
    upstream_customer_ids: list[int] = Field(default_factory=list)
    downstream_customer_ids: list[int] = Field(default_factory=list)
    conditions: list[DataFieldCondition] = Field(default_factory=list)
    sort_field_id: int | None = None
    sort_direction: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class DataRecordLine(BaseModel):
    """A complete current record with current customer identity and typed values."""

    model_config = ConfigDict(extra="forbid")

    id: int
    business_date: date
    upstream_customer_id: int
    downstream_customer_id: int
    upstream_customer_name: str
    downstream_customer_name: str
    upstream_customer_deleted: bool
    downstream_customer_deleted: bool
    upstream_customer_name_source: str
    downstream_customer_name_source: str
    revenue: Decimal
    cost: Decimal
    gross_profit: Decimal
    gross_margin: Decimal | None
    custom_values: dict[int, CustomRecordValue] = Field(default_factory=dict)

    @property
    def record_id(self) -> int:
        """Expose the canonical record ID under an explicit query-layer name."""
        return self.id


class DataSearchSummary(BaseModel):
    """Aggregates over every visible and filtered row, never only one page."""

    row_count: int
    revenue: Decimal
    cost: Decimal
    gross_profit: Decimal
    gross_margin: Decimal | None


class DataSearchResult(BaseModel):
    items: list[DataRecordLine]
    total: int
    page: int
    page_size: int
    summary: DataSearchSummary


class TableVisibilitySummary(BaseModel):
    """Visible current-row count and business-date bounds for one table."""

    row_count: int
    date_from: date | None
    date_to: date | None


__all__: tuple[str, ...] = (
    "CustomRecordValue",
    "DataFieldCondition",
    "DataRecordLine",
    "DataRecordSearch",
    "DataSearchResult",
    "DataSearchSummary",
    "TableVisibilitySummary",
)
