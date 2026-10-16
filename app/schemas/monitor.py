"""系统监控响应模型。"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_serializer


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
        utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return utc_value.astimezone(UTC).isoformat()


class SystemMetricRead(_SystemMetricFields):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CurrentSnapshotRead(_SystemMetricFields):
    model_config = ConfigDict(from_attributes=True)

    db_healthy: bool
    redis_healthy: bool


__all__: tuple[str, ...] = ("CurrentSnapshotRead", "SystemMetricRead")
