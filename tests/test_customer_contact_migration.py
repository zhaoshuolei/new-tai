import importlib
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.customer_helpers import TEST_DATABASE_URL


async def test_downgrade_deduplicates_credit_codes_before_restoring_unique_index() -> (
    None
):
    migration = importlib.import_module(
        "migrations.versions.b46bfc5aafcd_customer_contacts"
    )
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    table_name = f"customer_downgrade_{uuid4().hex[:12]}"
    metadata = sa.MetaData()
    table = sa.Table(
        table_name,
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("credit_code", sa.String(length=18), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "active_credit_code",
            sa.String(length=18),
            sa.Computed(
                "CASE WHEN status = 'ACTIVE' THEN credit_code END",
                persisted=True,
            ),
        ),
    )
    active_index = sa.Index(
        f"ix_{table_name}_active_credit_code",
        table.c.active_credit_code,
        unique=True,
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                table.insert(),
                [
                    {
                        "id": 1,
                        "credit_code": "CODEA0000000000001",
                        "status": "ACTIVE",
                    },
                    {
                        "id": 2,
                        "credit_code": "CODEA0000000000001",
                        "status": "DELETED",
                    },
                    {
                        "id": 3,
                        "credit_code": "CODEB0000000000002",
                        "status": "DELETED",
                    },
                    {
                        "id": 4,
                        "credit_code": "CODEB0000000000002",
                        "status": "DELETED",
                    },
                ],
            )
            await connection.run_sync(
                lambda sync_connection: migration._deduplicate_credit_codes(
                    sync_connection,
                    table_name=table_name,
                )
            )
            rows = (
                await connection.execute(
                    sa.select(table.c.id, table.c.credit_code).order_by(table.c.id)
                )
            ).all()
            assert rows == [
                (1, "CODEA0000000000001"),
                (2, None),
                (3, "CODEB0000000000002"),
                (4, None),
            ]

            await connection.run_sync(active_index.drop)
            await connection.execute(
                sa.text(f"ALTER TABLE `{table_name}` DROP COLUMN active_credit_code")
            )
            await connection.run_sync(
                sa.Index(
                    f"ix_{table_name}_credit_code",
                    table.c.credit_code,
                    unique=True,
                ).create
            )
            indexes = await connection.run_sync(
                lambda sync_connection: sa.inspect(sync_connection).get_indexes(
                    table_name
                )
            )
            old_index = next(
                item
                for item in indexes
                if item["name"] == f"ix_{table_name}_credit_code"
            )
            assert old_index["unique"] == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()
