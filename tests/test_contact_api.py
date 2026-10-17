"""Contact and contact-function HTTP API tests."""

from typing import Any

import pytest
import pytest_asyncio

from app.core.audit_meta import get_audit_route_meta
from app.models.contact import ContactStatus
from app.schemas.contact import ContactFunctionCreate, ContactFunctionUpdate
from app.services import audit as audit_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (  # noqa: F401
    api_client,
    make_context,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)


@pytest_asyncio.fixture(autouse=True)
async def disable_audit_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_service, "record_audit_entry", noop)


@pytest.mark.parametrize("model", [ContactFunctionCreate, ContactFunctionUpdate])
def test_function_name_schema_documents_length_without_runtime_constraints(
    model: type[ContactFunctionCreate] | type[ContactFunctionUpdate],
) -> None:
    name_schema = model.model_json_schema()["properties"]["name"]

    assert name_schema["minLength"] == 1
    assert name_schema["maxLength"] == 64


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
async def test_shared_function_name_validation_surfaces_7003(
    session,
    name: str,
) -> None:
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.post(
            "/api/v1/contact-functions",
            json={"name": name},
        )

    assert response.status_code == 422
    assert response.json()["code"] == 7003


async def test_shared_function_write_requires_super_admin(session) -> None:
    user = await make_user(session)
    ctx = make_context(user, permission_codes={"customer:update", "customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.post(
            "/api/v1/contact-functions",
            json={"name": "商务"},
        )
    assert response.status_code == 403


async def test_super_admin_creates_shared_function(session) -> None:
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.post(
            "/api/v1/contact-functions",
            json={"name": "商务"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "商务"
    assert response.json()["data"]["customer_id"] is None
    assert response.json()["data"]["scope"] == "SHARED"


async def test_custom_function_requires_customer_update(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.post(
            f"/api/v1/customers/{customer.id}/contact-functions",
            json={"name": "渠道"},
        )
    assert response.status_code == 403


async def test_custom_function_crud_and_restore(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(
        user,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    async with api_client(session, ctx) as client:
        created_response = await client.post(
            f"/api/v1/customers/{customer.id}/contact-functions",
            json={"name": "渠道"},
        )
        assert created_response.status_code == 200
        created = created_response.json()["data"]
        assert created["scope"] == "CUSTOM"

        updated_response = await client.patch(
            f"/api/v1/customers/{customer.id}/contact-functions/{created['id']}",
            json={"name": "合作伙伴"},
        )
        assert updated_response.status_code == 200
        assert updated_response.json()["data"]["name"] == "合作伙伴"

        deleted_response = await client.delete(
            f"/api/v1/customers/{customer.id}/contact-functions/{created['id']}"
        )
        assert deleted_response.status_code == 200

        recycle_response = await client.get(
            f"/api/v1/customers/{customer.id}/contact-functions/recycle-bin"
        )
        assert recycle_response.status_code == 200
        assert [item["id"] for item in recycle_response.json()["data"]] == [
            created["id"]
        ]

        restored_response = await client.post(
            f"/api/v1/customers/{customer.id}/contact-functions/{created['id']}/restore"
        )
        assert restored_response.status_code == 200
        assert restored_response.json()["data"]["name"] == "合作伙伴"


async def test_contact_crud_list_and_restore(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    ctx = make_context(
        user,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    async with api_client(session, ctx) as client:
        created_response = await client.post(
            f"/api/v1/customers/{customer.id}/contacts",
            json={"name": "张三", "function_id": shared.id, "phone": "138 0000 0000"},
        )
        assert created_response.status_code == 200
        created = created_response.json()["data"]
        assert created["function_name"] == "商务"
        assert created["function_scope"] == "SHARED"

        list_response = await client.get(f"/api/v1/customers/{customer.id}/contacts")
        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()["data"]["items"]] == [
            created["id"]
        ]

        updated_response = await client.patch(
            f"/api/v1/customers/{customer.id}/contacts/{created['id']}",
            json={"position": "采购"},
        )
        assert updated_response.status_code == 200
        assert updated_response.json()["data"]["position"] == "采购"

        deleted_response = await client.delete(
            f"/api/v1/customers/{customer.id}/contacts/{created['id']}"
        )
        assert deleted_response.status_code == 200

        recycle_response = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/recycle-bin"
        )
        assert recycle_response.status_code == 200
        assert [item["id"] for item in recycle_response.json()["data"]["items"]] == [
            created["id"]
        ]

        restored_response = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{created['id']}/restore"
        )
        assert restored_response.status_code == 200
        assert restored_response.json()["data"]["name"] == "张三"


async def test_contact_delete_requires_customer_delete_not_update(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, shared, name="张三")
    ctx = make_context(user, permission_codes={"customer:read", "customer:update"})
    async with api_client(session, ctx) as client:
        response = await client.delete(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
        )
    assert response.status_code == 403


async def test_contacts_page_size_above_limit_is_rejected(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.get(
            f"/api/v1/customers/{customer.id}/contacts",
            params={"page_size": 101},
        )
    assert response.status_code == 422


async def test_contacts_page_beyond_range_returns_empty_not_500(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(user, permission_codes={"customer:read"})
    async with api_client(session, ctx) as client:
        response = await client.get(
            f"/api/v1/customers/{customer.id}/contacts",
            params={"page": 99999},
        )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == []
    assert body["total"] == 0


async def test_contact_recycle_bin_route_not_shadowed_by_id(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    ctx = make_context(
        user,
        permission_codes={"customer:read", "customer:delete"},
    )
    async with api_client(session, ctx) as client:
        response = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/recycle-bin"
        )
    assert response.status_code == 200


async def test_function_recycle_bin_route_not_shadowed_by_id(session) -> None:
    user = await make_user(session)
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.get("/api/v1/contact-functions/recycle-bin")
    assert response.status_code == 200


async def test_super_admin_function_recycle_bin_returns_only_deleted(session) -> None:
    user = await make_user(session)
    await add_function(session, name="商务", customer_id=None)
    deleted = await add_function(
        session,
        name="旧职能",
        customer_id=None,
        status=ContactStatus.DELETED,
    )
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.get("/api/v1/contact-functions/recycle-bin")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [deleted.id]


async def test_non_super_admin_function_recycle_bin_is_forbidden(session) -> None:
    user = await make_user(session)
    ctx = make_context(
        user,
        permission_codes={"customer:read", "customer:delete"},
    )
    async with api_client(session, ctx) as client:
        response = await client.get("/api/v1/contact-functions/recycle-bin")
    assert response.status_code == 403


async def test_delete_function_error_surfaces_over_http(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    shared = await add_function(session, name="商务", customer_id=None)
    await add_contact(session, customer, shared, name="张三")
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.delete(f"/api/v1/contact-functions/{shared.id}")
    assert response.status_code == 422
    assert response.json()["code"] == 7008


async def test_name_conflict_error_surfaces_over_http(session) -> None:
    user = await make_user(session)
    await add_function(session, name="商务", customer_id=None)
    ctx = make_context(user, is_super_admin=True)
    async with api_client(session, ctx) as client:
        response = await client.post(
            "/api/v1/contact-functions",
            json={"name": "商务"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == 7007


@pytest.mark.parametrize(
    ("method", "template", "action", "resource_type", "resource_id_param"),
    [
        (
            "POST",
            "/contact-functions",
            "contact_function:create",
            "contact_function",
            None,
        ),
        (
            "PATCH",
            "/contact-functions/{function_id}",
            "contact_function:update",
            "contact_function",
            "function_id",
        ),
        (
            "DELETE",
            "/contact-functions/{function_id}",
            "contact_function:delete",
            "contact_function",
            "function_id",
        ),
        (
            "POST",
            "/contact-functions/{function_id}/restore",
            "contact_function:restore",
            "contact_function",
            "function_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contact-functions",
            "contact_function:create",
            "contact_function",
            None,
        ),
        (
            "PATCH",
            "/customers/{customer_id}/contact-functions/{function_id}",
            "contact_function:update",
            "contact_function",
            "function_id",
        ),
        (
            "DELETE",
            "/customers/{customer_id}/contact-functions/{function_id}",
            "contact_function:delete",
            "contact_function",
            "function_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contact-functions/{function_id}/restore",
            "contact_function:restore",
            "contact_function",
            "function_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contacts",
            "contact:create",
            "customer_contact",
            None,
        ),
        (
            "PATCH",
            "/customers/{customer_id}/contacts/{contact_id}",
            "contact:update",
            "customer_contact",
            "contact_id",
        ),
        (
            "DELETE",
            "/customers/{customer_id}/contacts/{contact_id}",
            "contact:delete",
            "customer_contact",
            "contact_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contacts/{contact_id}/restore",
            "contact:restore",
            "customer_contact",
            "contact_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/restore",
            "customer:restore",
            "customer",
            None,
        ),
    ],
)
def test_write_routes_are_registered_for_audit(
    method: str,
    template: str,
    action: str,
    resource_type: str,
    resource_id_param: str | None,
) -> None:
    meta = get_audit_route_meta(method, template)
    assert meta is not None
    assert meta.action == action
    assert meta.resource_type == resource_type
    assert meta.resource_id_param == resource_id_param


async def test_all_contact_routes_require_permission(session) -> None:
    user = await make_user(session)
    customer = await make_customer_row(session, owner=user)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function, name="张三")
    ctx = make_context(user)
    function_payload = {"name": "无权限"}
    contact_payload = {"name": "无权限", "function_id": function.id}
    routes = (
        ("GET", "/contact-functions", None),
        ("GET", "/contact-functions/recycle-bin", None),
        ("POST", "/contact-functions", function_payload),
        ("PATCH", f"/contact-functions/{function.id}", function_payload),
        ("DELETE", f"/contact-functions/{function.id}", None),
        ("POST", f"/contact-functions/{function.id}/restore", None),
        ("GET", f"/customers/{customer.id}/contact-functions", None),
        (
            "POST",
            f"/customers/{customer.id}/contact-functions",
            function_payload,
        ),
        ("GET", f"/customers/{customer.id}/contact-functions/recycle-bin", None),
        (
            "PATCH",
            f"/customers/{customer.id}/contact-functions/{function.id}",
            {"name": "无权限"},
        ),
        (
            "DELETE",
            f"/customers/{customer.id}/contact-functions/{function.id}",
            None,
        ),
        (
            "POST",
            f"/customers/{customer.id}/contact-functions/{function.id}/restore",
            None,
        ),
        ("GET", f"/customers/{customer.id}/contacts", None),
        ("POST", f"/customers/{customer.id}/contacts", contact_payload),
        ("GET", f"/customers/{customer.id}/contacts/recycle-bin", None),
        (
            "PATCH",
            f"/customers/{customer.id}/contacts/{contact.id}",
            {"position": "采购"},
        ),
        ("DELETE", f"/customers/{customer.id}/contacts/{contact.id}", None),
        (
            "POST",
            f"/customers/{customer.id}/contacts/{contact.id}/restore",
            None,
        ),
    )
    assert len(routes) == 18

    async with api_client(session, ctx) as client:
        for method, path, body in routes:
            kwargs = {"json": body} if body is not None else {}
            response = await client.request(
                method,
                f"/api/v1{path}",
                **kwargs,
            )
            assert response.status_code == 403, (method, path, response.text)
