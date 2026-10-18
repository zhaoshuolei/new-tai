import asyncio

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.errors import BusinessError, ErrorCode
from app.models.audit_log import AuditLog
from app.models.contact import ContactFunction, CustomerContact
from app.models.customer import Customer
from app.models.follow_up import (
    ContactFollowUp,
    ContactFollowUpAttachment,
    ContactFollowUpMethod,
)
from app.models.user import User
from app.services import follow_up_attachment as attachment_service
from app.services import follow_up_attachment_storage as storage
from tests.contact_helpers import add_contact, add_function
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    make_context,
    make_customer_row,
    make_user,
)
from tests.follow_up_helpers import add_attachment, add_follow_up, add_method

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


async def test_concurrent_uploads_cannot_exceed_attachment_limit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个会话同时上传时，只能有一个附件占用第五条名额。"""
    monkeypatch.setattr(
        settings, "follow_up_attachment_root", str(tmp_path / "attachments")
    )
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    customer_id: int | None = None
    function_id: int | None = None
    contact_id: int | None = None
    method_id: int | None = None
    follow_up_id: int | None = None
    uploaded_paths: list[str] = []

    try:
        async with factory() as setup:
            owner = await make_user(setup)
            customer = await make_customer_row(setup, owner=owner)
            function = await add_function(
                setup,
                name=f"并发附件-{owner.id}",
                customer_id=None,
            )
            contact = await add_contact(setup, customer, function)
            method = await add_method(setup, name=f"并发上传-{owner.id}")
            follow_up = await add_follow_up(
                setup,
                contact,
                method,
                follow_up_user=owner,
                content="已有四个附件",
            )
            for index in range(4):
                await add_attachment(
                    setup,
                    follow_up,
                    original_name=f"已有-{index}.pdf",
                )

            user_id = owner.id
            customer_id = customer.id
            function_id = function.id
            contact_id = contact.id
            method_id = method.id
            follow_up_id = follow_up.id
            context = make_context(
                owner,
                permission_codes={"customer:read", "customer:update"},
            )
            await setup.commit()

        guards_done = 0
        guards_lock = asyncio.Lock()
        both_guards_done = asyncio.Event()
        original_owned_guard = attachment_service._require_owned_active_follow_up

        async def synchronized_owned_guard(*args, **kwargs):
            nonlocal guards_done
            follow_up = await original_owned_guard(*args, **kwargs)
            async with guards_lock:
                guards_done += 1
                if guards_done == 2:
                    both_guards_done.set()
            await asyncio.wait_for(both_guards_done.wait(), timeout=5)
            return follow_up

        monkeypatch.setattr(
            attachment_service,
            "_require_owned_active_follow_up",
            synchronized_owned_guard,
        )

        async def upload(index: int) -> tuple[str, str | None, ErrorCode | None]:
            async def chunks():
                yield PDF

            async with factory() as session:
                try:
                    attachment = await attachment_service.upload_attachment(
                        session,
                        customer_id,
                        contact_id,
                        follow_up_id,
                        content_type="application/pdf",
                        original_name=f"并发-{index}.pdf",
                        chunks=chunks(),
                        context=context,
                    )
                except BusinessError as error:
                    return "error", None, error.code
                return "success", attachment.storage_path, None

        results = await asyncio.wait_for(
            asyncio.gather(upload(1), upload(2)),
            timeout=10,
        )
        uploaded_paths = [
            storage_path
            for status, storage_path, _ in results
            if status == "success" and storage_path is not None
        ]

        assert sorted(status for status, _, _ in results) == [
            "error",
            "success",
        ], results
        error_code = next(
            code for status, _, code in results if status == "error"
        )
        assert error_code == ErrorCode.FOLLOW_UP_ATTACHMENT_LIMIT

        async with factory() as check:
            total = await check.scalar(
                select(func.count())
                .select_from(ContactFollowUpAttachment)
                .where(ContactFollowUpAttachment.follow_up_id == follow_up_id)
            )
            assert total == 5
    finally:
        try:
            async with factory() as cleanup:
                if follow_up_id is not None:
                    await cleanup.execute(
                        delete(ContactFollowUpAttachment).where(
                            ContactFollowUpAttachment.follow_up_id == follow_up_id
                        )
                    )
                if user_id is not None:
                    await cleanup.execute(
                        delete(AuditLog).where(AuditLog.user_id == user_id)
                    )
                if follow_up_id is not None:
                    await cleanup.execute(
                        delete(ContactFollowUp).where(
                            ContactFollowUp.id == follow_up_id
                        )
                    )
                if contact_id is not None:
                    await cleanup.execute(
                        delete(CustomerContact).where(
                            CustomerContact.id == contact_id
                        )
                    )
                if method_id is not None:
                    await cleanup.execute(
                        delete(ContactFollowUpMethod).where(
                            ContactFollowUpMethod.id == method_id
                        )
                    )
                if function_id is not None:
                    await cleanup.execute(
                        delete(ContactFunction).where(
                            ContactFunction.id == function_id
                        )
                    )
                if customer_id is not None:
                    await cleanup.execute(
                        delete(Customer).where(Customer.id == customer_id)
                    )
                if user_id is not None:
                    await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
        finally:
            for storage_path in uploaded_paths:
                path = storage.resolve_attachment_path(storage_path)
                if path is not None:
                    path.unlink(missing_ok=True)
            await engine.dispose()
