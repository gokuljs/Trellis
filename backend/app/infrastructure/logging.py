from __future__ import annotations

import json
import logging
import math
import re
import sys
from datetime import UTC, datetime
from typing import Final

_EVENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.]{0,95}$")
_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LOGGER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_ROUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/[A-Za-z0-9_{}:/-]{0,199}$")
_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"}
)
_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "completed",
        "started",
        "rejected",
        "failed",
        "error",
        "cancelled",
        "interrupted",
        "fallback",
    }
)
_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "auth.authenticate",
        "run.start",
        "run.resume",
        "run.cancel",
        "run.respond",
        "unknown",
    }
)
_FORMAT_FAILURE: Final[str] = (
    '{"timestamp":"1970-01-01T00:00:00.000Z","level":"error",'
    '"logger":"app.infrastructure.logging","event":"log.format_failed"}'
)
_SAFE_FIELDS: Final[tuple[str, ...]] = (
    "route",
    "method",
    "status_code",
    "duration_ms",
    "outcome",
    "error_code",
    "command",
    "chat_id",
    "turn_id",
    "run_id",
)


class JsonLogFormatter(logging.Formatter):
    """Render only fixed event names and explicitly allowlisted metadata."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format_record(record)
        except Exception:
            return _FORMAT_FAILURE

    def _format_record(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        if not isinstance(event, str) or _EVENT_PATTERN.fullmatch(event) is None:
            event = "log.record"
        logger_name = record.name if _LOGGER_PATTERN.fullmatch(record.name) else "application"
        level = record.levelname.lower()
        if level not in {"debug", "info", "warning", "error", "critical"}:
            level = "info"
        payload: dict[str, str | int | float] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "logger": logger_name,
            "event": event,
        }
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                continue
            safe_value = _safe_field(field, value)
            if safe_value is not None:
                payload[field] = safe_value
        return json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _safe_field(field: str, value: object) -> str | int | float | None:
    if field == "route" and isinstance(value, str) and _ROUTE_PATTERN.fullmatch(value):
        return value
    if field == "method" and isinstance(value, str):
        return value if value in _METHODS else "_OTHER"
    if field == "status_code" and isinstance(value, int) and not isinstance(value, bool):
        return value if 100 <= value <= 599 else None
    if field == "duration_ms" and isinstance(value, (int, float)) and not isinstance(value, bool):
        duration = float(value)
        return round(duration, 3) if math.isfinite(duration) and duration >= 0 else None
    if field == "outcome" and isinstance(value, str) and value in _OUTCOMES:
        return value
    if field == "error_code" and isinstance(value, str) and _CODE_PATTERN.fullmatch(value):
        return value
    if field == "command" and isinstance(value, str) and value in _COMMANDS:
        return value
    if (
        field in {"chat_id", "turn_id", "run_id"}
        and isinstance(value, str)
        and _IDENTIFIER_PATTERN.fullmatch(value)
    ):
        return value
    return None


class _TrellisStreamHandler(logging.StreamHandler):
    trellis_json_handler = True


def configure_logging() -> None:
    """Install one JSON console handler and suppress unsafe Uvicorn access logs."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = next(
        (
            candidate
            for candidate in root.handlers
            if getattr(candidate, "trellis_json_handler", False)
        ),
        None,
    )
    if handler is None:
        for candidate in tuple(root.handlers):
            if candidate.__class__.__module__ != "_pytest.logging":
                root.removeHandler(candidate)
                candidate.close()
        handler = _TrellisStreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        root.addHandler(handler)

    uvicorn_error = logging.getLogger("uvicorn.error")
    for candidate in tuple(uvicorn_error.handlers):
        if not getattr(candidate, "trellis_json_handler", False):
            uvicorn_error.removeHandler(candidate)
    uvicorn_error.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
