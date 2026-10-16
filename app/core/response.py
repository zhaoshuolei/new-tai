from typing import Any

from pydantic import BaseModel


def success(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


__all__: tuple[str, ...] = ("Page", "success")
