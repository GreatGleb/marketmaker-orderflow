from typing import Any

from pydantic import BaseModel


class BaseResponse(BaseModel):
    ok: bool = True
    data: Any | None = None
    error: Any | None = None


class BasePaginateResponse(BaseResponse):
    total_count: int | None = None
    count: int | None = None
    page_count: int | None = None
