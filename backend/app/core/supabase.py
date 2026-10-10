"""Validate the single Supabase project endpoint used by Auth and PostgREST."""

import re
from urllib.parse import urlsplit

from app.core.config import Settings

_HOSTED_PROJECT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co\Z")
_PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]+\Z")


def project_base_url(settings: Settings) -> str | None:
    raw_url = (settings.supabase_url or "").strip()
    key = (settings.supabase_publishable_key or "").strip()
    if not raw_url or _PUBLISHABLE_KEY.fullmatch(key) is None:
        return None
    if "?" in raw_url or "#" in raw_url or "\\" in raw_url:
        return None
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or (port is not None and port < 1)
    ):
        return None
    if parts.scheme == "https" and (
        _HOSTED_PROJECT.fullmatch(parts.hostname) is None or port not in {None, 443}
    ):
        return None
    if parts.scheme == "http" and not (
        settings.environment == "development" and parts.hostname in {"localhost", "127.0.0.1"}
    ):
        return None
    return f"{parts.scheme}://{parts.netloc}"
