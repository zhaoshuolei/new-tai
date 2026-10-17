"""路由审计元信息注册表。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteAuditMeta:
    action: str
    resource_type: str
    resource_id_param: str | None = None


@dataclass(frozen=True)
class ActorSnapshot:
    user_id: int | None
    username: str | None
    nickname: str | None


_registry: dict[tuple[str, str], RouteAuditMeta] = {}


def register_audit_route(
    method: str,
    path: str,
    *,
    action: str,
    resource_type: str,
    resource_id_param: str | None = None,
) -> None:
    """Register the audit action of a write route by method and path template."""
    _registry[(method.upper(), path)] = RouteAuditMeta(
        action=action,
        resource_type=resource_type,
        resource_id_param=resource_id_param,
    )


def get_audit_route_meta(method: str, route_path: str) -> RouteAuditMeta | None:
    """Return the audit metadata registered for a matched route, if any."""
    return _registry.get((method.upper(), route_path))


__all__: tuple[str, ...] = (
    "ActorSnapshot",
    "RouteAuditMeta",
    "get_audit_route_meta",
    "register_audit_route",
)
