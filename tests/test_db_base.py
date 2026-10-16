from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.db.base import Base


class TimestampProbe(Base):
    __tablename__ = "timestamp_probe"


def test_timestamp_columns_use_utc_datetime6_in_mysql_ddl() -> None:
    ddl = str(
        CreateTable(TimestampProbe.__table__).compile(
            dialect=mysql.dialect(),
        )
    ).upper()

    assert "CREATED_AT DATETIME(6)" in ddl
    assert "UPDATED_AT DATETIME(6)" in ddl
    assert ddl.count("DEFAULT (UTC_TIMESTAMP(6))") == 2
    assert "NOW()" not in ddl


def test_updated_at_onupdate_uses_utc_timestamp6() -> None:
    onupdate = TimestampProbe.__table__.c.updated_at.onupdate
    assert onupdate is not None
    onupdate_expression = onupdate.arg
    assert onupdate_expression is not None

    expression = str(
        onupdate_expression.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert expression == "UTC_TIMESTAMP(6)"
