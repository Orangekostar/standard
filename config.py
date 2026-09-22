from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _configure_proxy_environment() -> None:
    use_proxy = _env_flag("QUANT_USE_PROXY", default=False)
    proxy_keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]
    if not use_proxy:
        for key in proxy_keys:
            os.environ.pop(key, None)
        return

    proxy_url = str(os.getenv("QUANT_PROXY_URL", "") or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
    all_proxy_url = str(os.getenv("QUANT_ALL_PROXY_URL", "") or "").strip()
    if all_proxy_url:
        os.environ["ALL_PROXY"] = all_proxy_url
        os.environ["all_proxy"] = all_proxy_url
    elif proxy_url:
        os.environ.pop("ALL_PROXY", None)
        os.environ.pop("all_proxy", None)


_configure_proxy_environment()


def _resolve_cache_dir() -> Path:
    cache_raw = os.getenv("CACHE_DIR", "cache")
    cache_path = Path(cache_raw)
    if not cache_path.is_absolute():
        cache_path = BASE_DIR / cache_path
    return cache_path


def _resolve_market_db_path() -> Path:
    db_raw = os.getenv("MARKET_DB_PATH", "")
    if db_raw:
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        return db_path
    return _resolve_cache_dir() / "a_share_market.db"


@dataclass(frozen=True)
class Settings:
    tushare_token: str = os.getenv("TUSHARE_TOKEN", "")
    cache_dir: Path = _resolve_cache_dir()
    market_db_path: Path = _resolve_market_db_path()


settings = Settings()
