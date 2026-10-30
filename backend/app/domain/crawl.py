"""Normalized navigation scope for a bounded public crawl."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def scope_path(value: str) -> str:
    path = value
    for _ in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", path):
            raise ValueError("invalid crawl path escape")
        decoded = unquote(path, errors="strict")
        if decoded == path:
            break
        path = decoded
    else:
        raise ValueError("ambiguous crawl path")
    if (
        not path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise ValueError("ambiguous crawl path")
    return path


def normalize_crawl_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid crawl URL")
    parts = urlsplit(value)
    host = parts.hostname
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or "@" in parts.netloc
        or "%" in parts.netloc
        or parts.netloc.endswith(":")
        or parts.port not in {None, 80, 443}
    ):
        raise ValueError("invalid crawl authority")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.encode("idna").decode("ascii").lower()
        if (
            len(host) > 253
            or re.fullmatch(r"[0-9.]+", host)
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in host.split(".")
            )
        ):
            raise ValueError("invalid crawl host") from None
    else:
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("non-public crawl host")
        host = str(address)
    authority = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    if parts.port is not None and parts.port != default_port:
        authority += f":{parts.port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    scope_path(path)
    query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    result = urlunsplit((parts.scheme, authority, path, query, ""))
    if len(result.encode()) > 4096:
        raise ValueError("crawl URL exceeds limit")
    return result


@dataclass(frozen=True, slots=True)
class CrawlScope:
    origin: str
    path_prefix: str

    @classmethod
    def from_seed(cls, seed_url: str, path_prefix: str) -> tuple[str, CrawlScope]:
        seed = normalize_crawl_url(seed_url)
        if "?" in path_prefix or "#" in path_prefix or len(path_prefix) > 4096:
            raise ValueError("invalid crawl path prefix")
        prefix = scope_path(path_prefix).rstrip("/") or "/"
        parts = urlsplit(seed)
        scope = cls(f"{parts.scheme}://{parts.netloc}", prefix)
        scope.accept(seed)
        return seed, scope

    def accept(self, url: str) -> str:
        normalized = normalize_crawl_url(url)
        parts = urlsplit(normalized)
        path = scope_path(parts.path)
        if f"{parts.scheme}://{parts.netloc}" != self.origin or not (
            self.path_prefix == "/"
            or path == self.path_prefix
            or path.startswith(self.path_prefix + "/")
        ):
            raise ValueError("URL is outside crawl scope")
        return normalized
