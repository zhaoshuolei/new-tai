from typing import Annotated, Any

from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 100
PageNumber = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]


def success(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


class SuccessEnvelope[T](BaseModel):
    code: int = 0
    message: str = "ok"
    data: T


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: PageNumber
    page_size: PageNumber


__all__: tuple[str, ...] = (
    "MAX_PAGE_SIZE",
    "Page",
    "SuccessEnvelope",
    "success",
)
