import pytest

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.services import follow_up as follow_up_service
from app.services import follow_up_attachment as attachment_service
from app.services import follow_up_attachment_storage as storage
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import make_context, make_customer_row, make_user, session  # noqa: F401
from tests.follow_up_helpers import add_follow_up, add_method

__all__ = ("session",)

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


async def _fixture(session, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        settings, "follow_up_attachment_root", str(tmp_path / "attachments")
    )
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner)
    function = await add_function(session, name="商务", customer_id=None)
    contact = await add_contact(session, customer, function)
    method = await add_method(session, name="电话")
    follow_up = await add_follow_up(
        session, contact, method, follow_up_user=owner, content="带附件"
    )
    ctx = make_context(
        owner,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )
    return owner, customer, contact, follow_up, ctx


async def _upload_pdf(session, customer, contact, follow_up, ctx, *, name="报价单.pdf"):
    async def chunks():
        yield PDF

    return await attachment_service.upload_attachment(
        session,
        customer.id,
        contact.id,
        follow_up.id,
        content_type="application/pdf",
        original_name=name,
        chunks=chunks(),
        context=ctx,
    )


async def test_upload_and_download_round_trip(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)

    assert attachment.original_name == "报价单.pdf"
    assert attachment.size_bytes == len(PDF)
    assert attachment.content_type == "application/pdf"

    path, loaded = await attachment_service.get_attachment_for_download(
        session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
    )
    assert path.read_bytes() == PDF
    assert loaded.id == attachment.id


async def test_sixth_attachment_is_rejected(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    for index in range(5):
        await _upload_pdf(
            session, customer, contact, follow_up, ctx, name=f"{index}.pdf"
        )

    with pytest.raises(BusinessError) as error:
        await _upload_pdf(session, customer, contact, follow_up, ctx, name="6.pdf")
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT


async def test_non_owner_cannot_upload_or_delete(
    session, tmp_path, monkeypatch
) -> None:  # noqa: F811
    owner, customer, contact, follow_up, owner_ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    attachment = await _upload_pdf(session, customer, contact, follow_up, owner_ctx)
    outsider = await make_user(session)
    ctx = make_context(
        outsider,
        permission_codes={"customer:read", "customer:update", "customer:delete"},
    )

    async def chunks():
        yield PDF

    with pytest.raises(BusinessError) as error:
        await attachment_service.upload_attachment(
            session,
            customer.id,
            contact.id,
            follow_up.id,
            content_type="application/pdf",
            original_name=None,
            chunks=chunks(),
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER

    with pytest.raises(BusinessError) as error:
        await attachment_service.delete_attachment(
            session,
            customer.id,
            contact.id,
            follow_up.id,
            attachment.id,
            context=ctx,
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_OWNER


async def test_non_owner_with_customer_read_can_download(
    session, tmp_path, monkeypatch
) -> None:  # noqa: F811
    owner, customer, contact, follow_up, owner_ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    attachment = await _upload_pdf(session, customer, contact, follow_up, owner_ctx)
    reader = await make_user(session)
    reader_ctx = make_context(reader, permission_codes={"customer:read"})

    path, loaded = await attachment_service.get_attachment_for_download(
        session,
        customer.id,
        contact.id,
        follow_up.id,
        attachment.id,
        context=reader_ctx,
    )

    assert path.read_bytes() == PDF
    assert loaded.id == attachment.id


async def test_deleted_attachment_file_is_gone(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)
    path = storage.resolve_attachment_path(attachment.storage_path)
    assert path is not None and path.exists()

    await attachment_service.delete_attachment(
        session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
    )
    assert not path.exists()
    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND


async def test_tampered_storage_path_cannot_escape_root(
    session, tmp_path, monkeypatch
) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    secret = media_root / "secret.pdf"
    secret.write_bytes(PDF)
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)
    attachment.storage_path = "../media/secret.pdf"
    await session.flush()

    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_ATTACHMENT_NOT_FOUND


async def test_deleted_record_hides_attachments(session, tmp_path, monkeypatch) -> None:  # noqa: F811
    owner, customer, contact, follow_up, ctx = await _fixture(
        session, tmp_path, monkeypatch
    )
    attachment = await _upload_pdf(session, customer, contact, follow_up, ctx)

    await follow_up_service.delete_follow_up(
        session, customer.id, contact.id, follow_up.id, context=ctx
    )
    with pytest.raises(BusinessError) as error:
        await attachment_service.get_attachment_for_download(
            session, customer.id, contact.id, follow_up.id, attachment.id, context=ctx
        )
    assert error.value.code == ErrorCode.FOLLOW_UP_NOT_FOUND
