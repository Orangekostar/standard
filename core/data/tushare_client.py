from __future__ import annotations

from typing import Any

from config import settings


def get_pro_api() -> Any | None:
    if not settings.tushare_token:
        return None
    try:
        import tushare as ts

        return ts.pro_api(settings.tushare_token)
    except Exception:
        return None
