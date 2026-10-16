"""系统监控响应模型。"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.exception_log import ExceptionLevel


def _as_utc_iso(value: datetime) -> str:
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc_value.astimezone(UTC).isoformat()


class _SystemMetricFields(BaseModel):
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_mb: float
    disk_total_mb: float
    process_memory_mb: float
    sampled_at: datetime

    @field_serializer("sampled_at", when_used="json")
    def serialize_sampled_at(self, value: datetime) -> str:
        return _as_utc_iso(value)


class SystemMetricRead(_SystemMetricFields):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CurrentSnapshotRead(_SystemMetricFields):
    model_config = ConfigDict(from_attributes=True)

    db_healthy: bool
    redis_healthy: bool


class ExceptionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exception_type: str
    message: str
    fingerprint: str
    level: ExceptionLevel
    request_id: str
    method: str
    path: str
    user_id: int | None
    status_code: int | None
    occurred_at: datetime
    acknowledged: bool
    acknowledged_by: int | None
    acknowledged_at: datetime | None

    @field_serializer("occurred_at", when_used="json")
    def serialize_occurred_at(self, value: datetime) -> str:
        return _as_utc_iso(value)

    @field_serializer("acknowledged_at", when_used="json")
    def serialize_acknowledged_at(self, value: datetime | None) -> str | None:
        return None if value is None else _as_utc_iso(value)


class ExceptionSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fingerprint: str
    exception_type: str
    level: ExceptionLevel
    count: int
    last_occurred_at: datetime
    acknowledged: bool

    @field_serializer("last_occurred_at", when_used="json")
    def serialize_last_occurred_at(self, value: datetime) -> str:
        return _as_utc_iso(value)


__all__: tuple[str, ...] = (
    "CurrentSnapshotRead",
    "ExceptionLogRead",
    "ExceptionSummaryRead",
    "SystemMetricRead",
)
