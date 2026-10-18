from datetime import datetime
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio

from app.core.audit_meta import get_audit_route_meta
from app.services import audit as audit_service
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (  # noqa: F401
    api_client,
    make_context,
    make_customer_row,
    make_user,
    session,
)
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)


@pytest_asyncio.fixture(autouse=True)
async def disable_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_service, "record_audit_entry", noop)


async def _fixture(session):  # noqa: F811
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function, name="张三")
    method = await add_method(session, name="电话")
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
        is_super_admin=True,
    )
    return owner, customer, contact, method, ctx


async def test_create_and_list_records(session) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    async with api_client(session, ctx) as client:
        created = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups",
            json={
                "method_id": method.id,
                "content": "电话沟通二期预算",
                "follow_up_at": "2026-10-18T06:30:00+00:00",
                "next_follow_up_at": "2026-10-21T02:00:00+00:00",
            },
        )
        assert created.status_code == 200
        body = created.json()["data"]
        assert body["method_name"] == "电话"
        assert body["contact_name"] == "张三"
        assert body["follow_up_user_id"] == owner.id
        assert body["follow_up_at"] == "2026-10-18T06:30:00+00:00"

        listed = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups"
        )
        data = listed.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == body["id"]


async def test_non_owner_gets_7017_from_api(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    author = await make_user(session)
    record = await add_follow_up(
        session, contact, method, follow_up_user=author, content="别人的"
    )
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    async with api_client(session, ctx) as client:
        response = await client.patch(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}",
            json={"content": "改"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == 7017


async def test_routes_require_permissions(session) -> None:  # noqa: F811
    owner, customer, contact, method, _ = await _fixture(session)
    ctx = make_context(owner)  # 无任何权限码
    async with api_client(session, ctx) as client:
        for method_name, url in (
            (
                "GET",
                f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups",
            ),
            (
                "POST",
                f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups",
            ),
            ("GET", "/api/v1/follow-ups/due"),
            ("GET", "/api/v1/follow-up-methods"),
            (
                "GET",
                f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
                "/follow-ups/1/attachments/1/download",
            ),
        ):
            response = await client.request(
                method_name,
                url,
                json={"method_id": method.id, "content": "x"},
            )
            assert response.status_code == 403, url

    read_update_ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update"},
    )
    async with api_client(session, read_update_ctx) as client:
        for method_name, url in (
            (
                "DELETE",
                f"/api/v1/customers/{customer.id}/contacts/{contact.id}/follow-ups/1",
            ),
            (
                "GET",
                f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
                "/follow-ups/recycle-bin",
            ),
        ):
            response = await client.request(method_name, url)
            assert response.status_code == 403, url


async def test_method_routes_deny_non_super_admin(session) -> None:  # noqa: F811
    owner, _, _, method, _ = await _fixture(session)
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update"},
    )
    async with api_client(session, ctx) as client:
        for method_name, url, payload in (
            ("POST", "/api/v1/follow-up-methods", {"name": "钉钉"}),
            (
                "PATCH",
                f"/api/v1/follow-up-methods/{method.id}",
                {"name": "微信"},
            ),
        ):
            response = await client.request(
                method_name,
                url,
                json=payload,
            )
            assert response.status_code == 403, url


async def test_due_summary_and_customers_endpoints(
    session,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: F811
    owner, customer, contact, method, ctx = await _fixture(session)
    await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        follow_up_at=datetime(2026, 10, 17, 16, 0),
        next_follow_up_at=datetime(2026, 10, 17, 15, 0),
    )
    monkeypatch.setattr(
        "app.services.follow_up._now_naive",
        lambda: datetime(2026, 10, 17, 20, 0),
    )
    async with api_client(session, ctx) as client:
        summary = await client.get("/api/v1/follow-ups/due/summary")
        assert summary.json()["data"] == {"today": 0, "overdue": 1}

        customers = await client.get("/api/v1/follow-ups/due/customers")
        items = customers.json()["data"]["items"]
        assert items[0]["customer_id"] == customer.id
        assert items[0]["due_count"] == 1
        assert items[0]["overdue_count"] == 1


async def test_attachment_upload_and_download(
    session,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "follow_up_attachment_root",
        str(tmp_path / "attachments"),
    )
    owner, customer, contact, method, ctx = await _fixture(session)
    record = await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        content="附件",
    )
    pdf = b"%PDF-1.7\n%%EOF\n"
    async with api_client(session, ctx) as client:
        upload = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments",
            content=pdf,
            headers={
                "Content-Type": "application/pdf",
                "X-File-Name": "%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf",
            },
        )
        assert upload.status_code == 200
        attachment = upload.json()["data"]
        assert attachment["original_name"] == "报价单.pdf"

        download = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments/{attachment['id']}/download"
        )
        assert download.status_code == 200
        assert download.content == pdf
        expected_filename = quote("报价单.pdf")
        assert (
            f"filename*=utf-8''{expected_filename}"
            in download.headers["content-disposition"]
        )
        assert download.headers["content-type"] == "application/pdf"
        assert download.headers["cache-control"] == "no-store"


async def test_attachment_download_fallback_preserves_detected_extension(
    session,
    tmp_path,
    monkeypatch,
) -> None:  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "follow_up_attachment_root",
        str(tmp_path / "attachments"),
    )
    owner, customer, contact, method, ctx = await _fixture(session)
    record = await add_follow_up(
        session,
        contact,
        method,
        follow_up_user=owner,
        content="无文件名附件",
    )
    pdf = b"%PDF-1.7\n%%EOF\n"
    async with api_client(session, ctx) as client:
        upload = await client.post(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments",
            content=pdf,
            headers={"Content-Type": "application/pdf"},
        )
        assert upload.status_code == 200
        attachment = upload.json()["data"]
        assert attachment["original_name"] is None

        download = await client.get(
            f"/api/v1/customers/{customer.id}/contacts/{contact.id}"
            f"/follow-ups/{record.id}/attachments/{attachment['id']}/download"
        )
        assert download.status_code == 200
        assert download.content == pdf
        content_disposition = download.headers["content-disposition"]
        assert "attachment-" in content_disposition
        filename = content_disposition.partition('filename="')[2].removesuffix('"')
        assert filename == f"attachment-{attachment['id']}.pdf"
        assert filename.endswith(".pdf")


@pytest.mark.parametrize(
    ("method", "template", "action", "resource_type", "resource_id_param"),
    [
        (
            "POST",
            "/follow-up-methods",
            "follow_up_method:create",
            "contact_follow_up_method",
            None,
        ),
        (
            "PATCH",
            "/follow-up-methods/{method_id}",
            "follow_up_method:update",
            "contact_follow_up_method",
            "method_id",
        ),
        (
            "DELETE",
            "/follow-up-methods/{method_id}",
            "follow_up_method:delete",
            "contact_follow_up_method",
            "method_id",
        ),
        (
            "POST",
            "/follow-up-methods/{method_id}/restore",
            "follow_up_method:restore",
            "contact_follow_up_method",
            "method_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contacts/{contact_id}/follow-ups",
            "contact_follow_up:create",
            "contact_follow_up",
            None,
        ),
        (
            "PATCH",
            "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
            "contact_follow_up:update",
            "contact_follow_up",
            "follow_up_id",
        ),
        (
            "DELETE",
            "/customers/{customer_id}/contacts/{contact_id}/follow-ups/{follow_up_id}",
            "contact_follow_up:delete",
            "contact_follow_up",
            "follow_up_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contacts/{contact_id}"
            "/follow-ups/{follow_up_id}/restore",
            "contact_follow_up:restore",
            "contact_follow_up",
            "follow_up_id",
        ),
        (
            "POST",
            "/customers/{customer_id}/contacts/{contact_id}"
            "/follow-ups/{follow_up_id}/attachments",
            "contact_follow_up:attachment_upload",
            "contact_follow_up_attachment",
            None,
        ),
        (
            "DELETE",
            "/customers/{customer_id}/contacts/{contact_id}"
            "/follow-ups/{follow_up_id}/attachments/{attachment_id}",
            "contact_follow_up:attachment_delete",
            "contact_follow_up_attachment",
            "attachment_id",
        ),
    ],
)
def test_write_routes_are_registered_for_audit(
    method,
    template,
    action,
    resource_type,
    resource_id_param,
) -> None:
    meta = get_audit_route_meta(method, template)
    assert meta is not None
    assert meta.action == action
    assert meta.resource_type == resource_type
    assert meta.resource_id_param == resource_id_param
