"""活跃客户名称唯一性。

覆盖生成列与唯一索引、四条写入路径（手工创建、天眼查回填、更新、恢复）、
只读重名检查脚本和迁移前置校验。全部离线：天眼查客户端以替身注入。
"""

import importlib
import os
import stat
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import Computed
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.errors import BusinessError, ErrorCode
from app.models.customer import Customer
from app.repositories import customer as customer_repository
from app.schemas.customer import CustomerCreate, CustomerUpdate
from app.services import customer as customer_service
from app.services.customer import (
    create_customer,
    delete_customer,
    import_from_tianyancha,
    restore_customer,
    update_customer,
)
from app.services.customer_name import (
    customer_name_lookup_key,
    normalize_customer_name,
)
from scripts.check_duplicate_customer_names import (
    DuplicateNameGroup,
    _duplicate_active_name_rows,
    emit_report,
    find_duplicate_active_names,
    render_report,
)
from tests.customer_helpers import (
    TEST_DATABASE_URL,
    FakeTianyanchaClient,
    make_context,
    make_customer_row,
    make_user,
    session,
)

__all__ = ("session",)
MIGRATION_MODULE = "migrations.versions.a7d3c9e15b42_customer_active_name"
REQUIRED_NAME_COLLATION = "utf8mb4_0900_ai_ci"


@pytest.fixture
def fake_client() -> FakeTianyanchaClient:
    return FakeTianyanchaClient()


@pytest.fixture(autouse=True)
def _inject_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: FakeTianyanchaClient,
) -> None:
    async def _get_client(_session: AsyncSession) -> FakeTianyanchaClient:
        return fake_client

    monkeypatch.setattr(customer_service, "get_tianyancha_client", _get_client)


def test_customer_name_exists_error_code_is_7022() -> None:
    assert ErrorCode.CUSTOMER_NAME_EXISTS == 7022


def test_active_name_is_generated_from_trimmed_name_and_unique() -> None:
    column = Customer.__table__.c.active_name
    assert isinstance(column.computed, Computed)
    computed = str(column.computed.sqltext)
    assert "TRIM(name)" in computed
    assert "status = 'ACTIVE'" in computed
    column_type = column.type
    assert isinstance(column_type, sa.String)
    assert column_type.collation == REQUIRED_NAME_COLLATION

    unique_columns = {
        tuple(item.name for item in index.columns)
        for index in Customer.__table__.indexes  # type: ignore[attr-defined]
        if index.unique
    }
    assert ("active_name",) in unique_columns


async def test_active_name_database_uses_required_collation(
    session: AsyncSession,
) -> None:
    collation = await session.scalar(
        sa.text(
            """
            SELECT COLLATION_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'customer'
              AND COLUMN_NAME = 'active_name'
            """
        )
    )

    assert collation == REQUIRED_NAME_COLLATION


@pytest.mark.parametrize(
    ("left", "right", "legacy_equal", "required_equal"),
    [
        # 旧规则把 BMP 外的两个字符合并，新规则区分它们（旧规则会误报重名）。
        ("𠀪", "𠀫", 1, 0),
        # 旧规则区分 æ 与 ae，新规则合并它们（旧规则会漏掉重名）。
        ("æ 客户", "ae 客户", 0, 1),
    ],
)
async def test_selected_pair_distinguishes_legacy_and_required_collations(
    session: AsyncSession,
    left: str,
    right: str,
    legacy_equal: int,
    required_equal: int,
) -> None:
    """所选字符对必须在两种排序规则下给出不同结论。"""
    actual = (
        await session.execute(
            sa.text(
                """
                SELECT
                    :left COLLATE utf8mb4_unicode_ci
                        = :right COLLATE utf8mb4_unicode_ci AS legacy,
                    :left COLLATE utf8mb4_0900_ai_ci
                        = :right COLLATE utf8mb4_0900_ai_ci AS required
                """
            ),
            {"left": left, "right": right},
        )
    ).one()

    assert (int(actual.legacy), int(actual.required)) == (
        legacy_equal,
        required_equal,
    )


def test_normalize_customer_name_trims_surrounding_whitespace() -> None:
    assert normalize_customer_name(" 客户甲 ") == "客户甲"


@pytest.mark.parametrize("value", ["", "   ", "\t\n "])
def test_normalize_customer_name_rejects_blank_name(value: str) -> None:
    with pytest.raises(BusinessError) as exc:
        normalize_customer_name(value)

    assert exc.value.code == ErrorCode.CUSTOMER_INVALID_FIELD
    assert exc.value.http_status == 422


def test_lookup_key_follows_case_and_accent_insensitive_intent() -> None:
    assert customer_name_lookup_key(" Café ") == customer_name_lookup_key("CAFE")
    assert customer_name_lookup_key("ＣＡＦÉ") == "cafe"


async def test_active_customer_name_is_unique(session: AsyncSession) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    await create_customer(
        session,
        CustomerCreate(name=" 客户甲 "),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="客户甲"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409


async def test_create_stores_trimmed_name(session: AsyncSession) -> None:
    owner = await make_user(session)
    context = make_context(owner)

    customer = await create_customer(
        session,
        CustomerCreate(name=" 客户甲 "),
        context=context,
    )

    assert customer.name == "客户甲"
    assert customer.active_name == "客户甲"


async def test_active_name_uniqueness_uses_database_collation(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    await create_customer(session, CustomerCreate(name="Café 客户"), context=context)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="cafe 客户"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS


async def test_active_name_uniqueness_does_not_use_legacy_collation(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    first = await create_customer(session, CustomerCreate(name="𠀪"), context=context)
    second = await create_customer(session, CustomerCreate(name="𠀫"), context=context)

    assert first.id != second.id
    assert first.active_name == "𠀪"
    assert second.active_name == "𠀫"


async def test_active_name_uniqueness_requires_0900_equivalence(
    session: AsyncSession,
) -> None:
    """``æ`` 与 ``ae`` 仅在新排序规则下相等，是旧规则会漏掉的重名。

    ``utf8mb4_unicode_ci`` 认为两者不同，若唯一索引沿用旧规则，第二行会被
    放行；``utf8mb4_0900_ai_ci`` 认为它们相等，因此必须报 7022。
    """
    owner = await make_user(session)
    context = make_context(owner)
    await create_customer(session, CustomerCreate(name="æ 客户"), context=context)

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name="ae 客户"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS


async def test_deleted_customer_releases_name(session: AsyncSession) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    first = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )

    await delete_customer(session, first.id, context=context)
    replacement = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )

    assert replacement.id != first.id
    assert replacement.name == "客户甲"
    assert replacement.active_name == "客户甲"


async def test_restore_conflicts_when_active_customer_reuses_name(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    deleted = await create_customer(
        session,
        CustomerCreate(name=" 客户甲 "),
        context=context,
    )
    await delete_customer(session, deleted.id, context=context)
    replacement = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, deleted.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409
    assert replacement.active_name == "客户甲"


async def test_restore_stores_trimmed_name(session: AsyncSession) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    customer = await make_customer_row(session, owner=owner, name=" 客户甲 ")
    await delete_customer(session, customer.id, context=context)

    restored = await restore_customer(session, customer.id, context=context)

    assert restored.name == "客户甲"
    assert restored.active_name == "客户甲"


async def test_update_to_duplicate_name_is_rejected(session: AsyncSession) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    await create_customer(session, CustomerCreate(name="客户甲"), context=context)
    second = await create_customer(
        session,
        CustomerCreate(name="客户乙"),
        context=context,
    )

    with pytest.raises(BusinessError) as exc:
        await update_customer(
            session,
            second.id,
            CustomerUpdate(name=" 客户甲 "),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409


async def test_update_stores_trimmed_name_and_keeps_own_name(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    customer = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )

    renamed = await update_customer(
        session,
        customer.id,
        CustomerUpdate(name=" 客户乙 "),
        context=context,
    )
    kept = await update_customer(
        session,
        customer.id,
        CustomerUpdate(name=" 客户乙 "),
        context=context,
    )

    assert renamed.name == "客户乙"
    assert renamed.active_name == "客户乙"
    assert kept.name == "客户乙"


async def test_tianyancha_import_stores_trimmed_name(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    fake_client.registration = replace(fake_client.registration, name=" 乐视网 ")

    customer = await import_from_tianyancha(
        session,
        fake_client.registration.credit_code,
        None,
        context=context,
    )

    assert customer.name == "乐视网"
    assert customer.active_name == "乐视网"


async def test_tianyancha_import_rejects_duplicate_name(
    session: AsyncSession,
    fake_client: FakeTianyanchaClient,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    fake_client.registration = replace(fake_client.registration, name="乐视网")
    await import_from_tianyancha(
        session,
        fake_client.registration.credit_code,
        None,
        context=context,
    )

    other_code = "91110000000000002X"
    fake_client.registration_credit_code = other_code
    with pytest.raises(BusinessError) as exc:
        await import_from_tianyancha(
            session,
            other_code,
            None,
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409


async def test_create_race_translates_integrity_error_to_7022(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    existing = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )

    original_lookup = customer_repository.get_active_by_name

    async def stale_then_current(
        current_session: AsyncSession,
        name: str,
        *,
        for_update: bool = False,
    ) -> Customer | None:
        if not for_update:
            return None
        return await original_lookup(
            current_session,
            name,
            for_update=for_update,
        )

    monkeypatch.setattr(
        customer_repository,
        "get_active_by_name",
        stale_then_current,
    )

    with pytest.raises(BusinessError) as exc:
        await create_customer(
            session,
            CustomerCreate(name=" 客户甲 "),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409
    assert existing.name == "客户甲"


async def test_update_race_translates_integrity_error_to_7022(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    first = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )
    second = await create_customer(
        session,
        CustomerCreate(name="客户乙"),
        context=context,
    )

    original_lookup = customer_repository.get_active_by_name

    async def stale_then_current(
        current_session: AsyncSession,
        name: str,
        *,
        for_update: bool = False,
    ) -> Customer | None:
        if not for_update:
            return None
        return await original_lookup(
            current_session,
            name,
            for_update=for_update,
        )

    monkeypatch.setattr(
        customer_repository,
        "get_active_by_name",
        stale_then_current,
    )

    with pytest.raises(BusinessError) as exc:
        await update_customer(
            session,
            second.id,
            CustomerUpdate(name="客户甲"),
            context=context,
        )

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409
    assert first.name == "客户甲"
    # 保存点回滚后对象已过期，刷新确认失败的更新没有落库。
    await session.refresh(second)
    assert second.name == "客户乙"


async def test_restore_race_translates_integrity_error_to_7022(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await make_user(session)
    context = make_context(owner)
    deleted = await create_customer(
        session,
        CustomerCreate(name="客户甲"),
        context=context,
    )
    await delete_customer(session, deleted.id, context=context)
    await create_customer(session, CustomerCreate(name="客户甲"), context=context)

    original_lookup = customer_repository.get_active_by_name
    lookup_count = 0
    lock_flags: list[bool] = []

    async def stale_then_current(
        current_session: AsyncSession,
        name: str,
        *,
        for_update: bool = False,
    ) -> Customer | None:
        nonlocal lookup_count
        lock_flags.append(for_update)
        if lookup_count == 0:
            lookup_count += 1
            return None
        return await original_lookup(
            current_session,
            name,
            for_update=for_update,
        )

    monkeypatch.setattr(
        customer_repository,
        "get_active_by_name",
        stale_then_current,
    )

    with pytest.raises(BusinessError) as exc:
        await restore_customer(session, deleted.id, context=context)

    assert exc.value.code == ErrorCode.CUSTOMER_NAME_EXISTS
    assert exc.value.http_status == 409
    # 冲突后的复核必须是当前读，否则 REPEATABLE READ 快照看不到并发提交。
    assert lock_flags == [False, True]


async def test_get_active_by_name_matches_trimmed_active_rows(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner, name=" 客户甲 ")
    await session.flush()

    found = await customer_repository.get_active_by_name(session, "客户甲")
    locked = await customer_repository.get_active_by_name(
        session,
        "客户甲",
        for_update=True,
    )

    assert found is not None
    assert found.id == customer.id
    assert locked is not None
    assert locked.id == customer.id
    assert await customer_repository.get_active_by_name(session, "客户乙") is None

    await customer_repository.soft_delete(session, customer, deleted_by=owner.id)
    await session.flush()

    assert await customer_repository.get_active_by_name(session, "客户甲") is None


async def test_get_active_by_name_uses_required_collation_explicitly(
    session: AsyncSession,
) -> None:
    owner = await make_user(session)
    customer = await make_customer_row(session, owner=owner, name="𠀪")
    await session.flush()

    assert await customer_repository.get_active_by_name(session, "𠀫") is None
    found = await customer_repository.get_active_by_name(session, "𠀪")
    assert found is not None
    assert found.id == customer.id


async def _create_scratch_customer_table(
    engine: AsyncEngine,
    table: sa.Table,
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(table.metadata.create_all)


async def test_migration_blocks_duplicate_active_names_without_names() -> None:
    migration = importlib.import_module(MIGRATION_MODULE)
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    table_name = f"customer_duplicates_{uuid4().hex[:12]}"
    metadata = sa.MetaData()
    table = sa.Table(
        table_name,
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "name",
            sa.String(length=255, collation="utf8mb4_unicode_ci"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
    )

    try:
        await _create_scratch_customer_table(engine, table)
        async with engine.begin() as connection:
            await connection.execute(
                table.insert(),
                [
                    {"id": 1, "name": " 客户甲 ", "status": "ACTIVE"},
                    {"id": 2, "name": "客户甲", "status": "ACTIVE"},
                    {"id": 3, "name": "客户乙", "status": "ACTIVE"},
                    {"id": 4, "name": "客户乙", "status": "DELETED"},
                    {"id": 5, "name": "客户丙", "status": "ACTIVE"},
                    {"id": 6, "name": "𠀪", "status": "ACTIVE"},
                    {"id": 7, "name": "𠀫", "status": "ACTIVE"},
                ],
            )
            with pytest.raises(RuntimeError) as exc:
                await connection.run_sync(
                    lambda sync_connection: migration._assert_no_duplicate_active_names(
                        sync_connection,
                        table_name=table_name,
                    )
                )
        message = str(exc.value)
        assert "1 duplicate group(s)" in message
        assert "客户甲" not in message
        assert "客户乙" not in message
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_script_lists_trimmed_duplicate_groups_with_ids() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    table_name = f"customer_duplicates_{uuid4().hex[:12]}"
    metadata = sa.MetaData()
    table = sa.Table(
        table_name,
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "name",
            sa.String(length=255, collation="utf8mb4_unicode_ci"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
    )

    try:
        await _create_scratch_customer_table(engine, table)
        async with engine.begin() as connection:
            await connection.execute(
                table.insert(),
                [
                    {"id": 1, "name": " 客户甲 ", "status": "ACTIVE"},
                    {"id": 2, "name": "客户甲", "status": "ACTIVE"},
                    {"id": 3, "name": "客户乙", "status": "ACTIVE"},
                    {"id": 4, "name": "客户乙", "status": "DELETED"},
                    {"id": 5, "name": "客户丙", "status": "ACTIVE"},
                ],
            )
        async with AsyncSession(engine) as check_session:
            groups = await _duplicate_active_name_rows(
                check_session,
                table_name=table_name,
            )
        # 只报告活跃客户；已删除的同名行不占用名称。
        assert groups == {"客户甲": [1, 2]}
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_script_groups_names_under_required_collation() -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    table_name = f"customer_duplicates_{uuid4().hex[:12]}"
    metadata = sa.MetaData()
    table = sa.Table(
        table_name,
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "name",
            sa.String(length=255, collation="utf8mb4_unicode_ci"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
    )

    try:
        await _create_scratch_customer_table(engine, table)
        async with engine.begin() as connection:
            await connection.execute(
                table.insert(),
                [
                    {"id": 1, "name": "ACME 客户", "status": "ACTIVE"},
                    {"id": 2, "name": "acme 客户", "status": "ACTIVE"},
                    {"id": 3, "name": "Other", "status": "ACTIVE"},
                    {"id": 4, "name": "𠀪", "status": "ACTIVE"},
                    {"id": 5, "name": "𠀫", "status": "ACTIVE"},
                ],
            )
        async with AsyncSession(engine) as check_session:
            groups = await _duplicate_active_name_rows(
                check_session,
                table_name=table_name,
            )
        # 名称列故意使用旧排序规则；检查必须显式采用 0900 规则。
        assert [sorted(ids) for ids in groups.values()] == [[1, 2]]
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_script_finds_no_duplicates_on_clean_database(
    session: AsyncSession,
) -> None:
    assert await find_duplicate_active_names(session) == []


def test_report_renders_groups_and_repair_command() -> None:
    report = render_report(
        [
            DuplicateNameGroup(name="客户甲", customer_ids=(7, 9)),
            DuplicateNameGroup(name="客户乙", customer_ids=(11, 13)),
        ]
    )

    assert "2 group(s)" in report
    assert "客户甲" in report
    assert "7, 9" in report
    assert "alembic upgrade head" in report


def test_report_without_duplicates_is_explicit() -> None:
    assert "No duplicate" in render_report([])


def test_report_writes_private_file_only(tmp_path, capsys) -> None:
    output = tmp_path / "duplicates.txt"

    emit_report("客户甲\n", output)

    assert output.read_text(encoding="utf-8") == "客户甲\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


def test_report_tightens_existing_file_permissions(tmp_path) -> None:
    output = tmp_path / "duplicates.txt"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)

    emit_report("new", output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_report_prints_to_stdout_without_output_path(capsys) -> None:
    emit_report("客户甲\n", None)

    assert capsys.readouterr().out == "客户甲\n"


def test_report_preserves_write_failure_without_double_closing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "duplicates.txt"
    # 脚本与测试引用同一个 os 模块对象：替换该引用即拦截脚本内部调用。
    real_close = os.close
    closed_descriptors: list[int] = []

    def tracking_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    class FailingWriter:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def __enter__(self) -> FailingWriter:
            return self

        def __exit__(self, *_args: object) -> None:
            tracking_close(self.descriptor)

        def write(self, _report: str) -> None:
            raise RuntimeError("simulated write failure")

    def failing_fdopen(
        descriptor: int,
        *_args: object,
        **_kwargs: object,
    ) -> FailingWriter:
        return FailingWriter(descriptor)

    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(os, "fdopen", failing_fdopen)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        emit_report("客户甲\n", output)

    assert len(closed_descriptors) == 1
