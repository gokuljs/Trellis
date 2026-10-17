from urllib.parse import urlsplit

_LOCAL_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000"})


def canonical_web_origin(value: str) -> bool:
    if not value or value != value.strip() or "\\" in value or "*" in value:
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return bool(
        parts.scheme == "https"
        and parts.hostname
        and parts.netloc == parts.netloc.lower()
        and parts.hostname.isascii()
        and not any(character.isspace() for character in value)
        and parts.username is None
        and parts.password is None
        and port not in {0, 443}
        and value == f"https://{parts.netloc}"
    )


def allowed_web_origins(environment: str, web_origin: str | None) -> frozenset[str]:
    origins = set(_LOCAL_ORIGINS) if environment in {"development", "test"} else set()
    if web_origin is not None and canonical_web_origin(web_origin):
        origins.add(web_origin)
    return frozenset(origins)
