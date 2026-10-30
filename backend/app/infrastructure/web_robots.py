"""Worker-only robots decisions over bounded, status-preserving public HTTP."""

import asyncio
import re
import ssl
import zlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import unquote_plus, urljoin

import h11

from app.application.secret_redaction import COMMON_SECRET_REDACTOR
from app.domain.retrieval import MAX_REDIRECTS, RetrievalRequest, RetrievalResult, RetrievalStatus
from app.domain.web import WebUsage
from app.infrastructure.web_network import (
    DestinationBlocked,
    PublicURL,
    parse_public_url,
    validate_scope,
)
from app.infrastructure.web_reader import _Decoder, _RetrievalFailure

if TYPE_CHECKING:
    from app.infrastructure.web_reader import RawDocumentReader

ROBOTS_MAX_BYTES = 512 * 1024
_MAX_HEADERS = 32768
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_AGENT = re.compile(r"[A-Za-z_-]+\Z")
_ESCAPE = re.compile(r"[0-9a-fA-F]{2}\Z")


def _safe_robots_url(value: str) -> PublicURL:
    decoded = value
    for _ in range(5):
        if COMMON_SECRET_REDACTOR.redact_text(decoded) != decoded:
            raise DestinationBlocked("robots_sensitive_url")
        following = unquote_plus(decoded, errors="strict")
        if following == decoded:
            return parse_public_url(value)
        decoded = following
    raise DestinationBlocked("robots_ambiguous_url")


def _comparison_path(value: str, *, pattern: bool) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        escape = value[index + 1 : index + 3]
        if character == "%" and _ESCAPE.fullmatch(escape) is not None:
            octet = int(escape, 16)
            result.append(chr(octet) if octet in _UNRESERVED else f"%{octet:02X}")
            index += 3
            continue
        literal_special = (not pattern and character in "*$") or (
            pattern and character == "$" and index != len(value) - 1
        )
        if ord(character) > 127 or character == "%" or literal_special:
            result.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
        else:
            result.append(character)
        index += 1
    return "".join(result)


def _matches(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    pieces = pattern.split("*")
    first = pieces[0]
    if not target.startswith(first):
        return False
    if len(pieces) == 1:
        return not anchored or len(target) == len(first)
    offset = len(first)
    for piece in pieces[1:-1]:
        found = target.find(piece, offset)
        if found < 0:
            return False
        offset = found + len(piece)
    last = pieces[-1]
    if anchored:
        return target.endswith(last) and len(target) - len(last) >= offset
    return target.find(last, offset) >= 0


@dataclass(frozen=True, slots=True)
class _Rule:
    path: str
    allow: bool

    @property
    def specificity(self) -> int:
        return len(self.path)


def robots_allows(body: bytes, target: str, product_token: str = "TrellisReader") -> bool:
    """Apply RFC 9309 groups and path rules without changing logical lines."""
    if len(body) > ROBOTS_MAX_BYTES:
        return False
    groups: list[tuple[list[str], list[_Rule]]] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    saw_rule = False
    text = body.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    for raw in text.split("\n"):
        line = raw.split("#", 1)[0].strip(" \t")
        if not line or ":" not in line or "\ufffd" in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.strip(" \t").lower(), value.strip(" \t")
        if name == "user-agent":
            if value != "*" and _AGENT.fullmatch(value) is None:
                continue
            if saw_rule:
                groups.append((agents, rules))
                agents, rules, saw_rule = [], [], False
            agents.append(value.lower())
        elif name in {"allow", "disallow"} and agents:
            saw_rule = True
            if not value or value[0] not in {"/", "*"}:
                continue
            if any(ord(character) < 33 or ord(character) == 127 for character in value):
                continue
            rules.append(_Rule(_comparison_path(value, pattern=True), name == "allow"))
    if agents:
        groups.append((agents, rules))
    token = product_token.lower()
    matched = [rule for names, entries in groups if token in names for rule in entries]
    if not any(token in names for names, _ in groups):
        matched = [rule for names, entries in groups if "*" in names for rule in entries]
    if target == "/robots.txt":
        return True
    path = _comparison_path(target, pattern=False)
    winning = max(
        (rule for rule in matched if _matches(rule.path, path)),
        key=lambda rule: (rule.specificity, rule.allow),
        default=None,
    )
    return winning is None or winning.allow


@dataclass(frozen=True, slots=True)
class _RobotsResponse:
    status: int
    body: bytes
    location: str | None = None


@dataclass(slots=True)
class _RobotsUsage:
    requests: int = 0
    transferred: int = 0


async def _fetch_robots_response(
    reader: RawDocumentReader,
    current: PublicURL,
    request: RetrievalRequest,
    usage: _RobotsUsage,
) -> _RobotsResponse:
    if usage.requests >= request.max_requests:
        raise _RetrievalFailure(RetrievalStatus.BUDGET_EXHAUSTED, "robots_request_limit")
    stream, writer = await reader._open(current)
    try:
        connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=_MAX_HEADERS)
        writer.write(
            connection.send(
                h11.Request(
                    method="GET",
                    target=current.target,
                    headers=[
                        ("Host", current.authority),
                        ("User-Agent", "TrellisReader/1.0"),
                        ("Accept", "text/plain"),
                        ("Accept-Encoding", "gzip, deflate"),
                        ("Connection", "close"),
                    ],
                )
            )
        )
        writer.write(connection.send(h11.EndOfMessage()))
        usage.requests += 1
        await writer.drain()
        response: h11.Response | None = None
        decoder: _Decoder | None = None
        body = bytearray()
        raw_bytes = 0
        informationals = 0
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                available = min(
                    request.max_response_bytes - usage.transferred,
                    ROBOTS_MAX_BYTES + 1 - raw_bytes,
                )
                if available <= 0:
                    raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_byte_limit")
                connection.receive_data(await stream.read(min(16384, available)))
            elif isinstance(event, h11.InformationalResponse):
                informationals += 1
                if informationals > 4 or event.status_code == 101:
                    raise ValueError("unsupported robots HTTP upgrade")
            elif isinstance(event, h11.Response):
                response = event
                headers: dict[str, str] = {}
                for name, value in event.headers:
                    name = name.decode("ascii").lower()
                    if name in headers and name in {"location", "content-encoding", "content-type"}:
                        raise ValueError("ambiguous robots response headers")
                    headers[name] = value.decode("latin-1")
                if sum(len(name) + len(value) for name, value in event.headers) > _MAX_HEADERS:
                    raise ValueError("robots response header limit")
                if event.status_code in _REDIRECTS:
                    return _RobotsResponse(event.status_code, b"", headers.get("location"))
                if not 200 <= event.status_code < 300:
                    return _RobotsResponse(event.status_code, b"")
                if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/plain":
                    raise ValueError("unsupported robots media type")
                if int(headers.get("content-length", "0")) > ROBOTS_MAX_BYTES:
                    raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_byte_limit")
                decoder = _Decoder(
                    headers.get("content-encoding", "identity").strip().lower(), ROBOTS_MAX_BYTES
                )
            elif isinstance(event, h11.Data):
                raw_bytes += len(event.data)
                usage.transferred += len(event.data)
                if raw_bytes > ROBOTS_MAX_BYTES or usage.transferred > request.max_response_bytes:
                    raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_byte_limit")
                if decoder is None:
                    raise ValueError("robots body before response")
                body.extend(decoder.decode(event.data))
            elif isinstance(event, h11.EndOfMessage):
                if response is None or decoder is None:
                    raise ValueError("incomplete robots response")
                decoder.finish()
                return _RobotsResponse(response.status_code, bytes(body))
            elif isinstance(event, h11.ConnectionClosed):
                raise ValueError("incomplete robots response")
            else:
                raise ValueError("unexpected robots HTTP event")
    finally:
        await reader._close(writer)


async def retrieve_crawl_document(
    reader: RawDocumentReader, request: RetrievalRequest
) -> RetrievalResult:
    """Fetch robots and one non-redirecting page within the same worker allowance."""
    usage = _RobotsUsage()
    current: PublicURL | None = None
    failure_status = RetrievalStatus.BLOCKED
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(request.timeout_seconds):
            current = _safe_robots_url(request.url)
            validate_scope(current, request.scope_origin, request.scope_path_prefix)
            robots = parse_public_url(current.origin + "/robots.txt")
            for redirect in range(MAX_REDIRECTS + 1):
                response = await _fetch_robots_response(reader, robots, request, usage)
                if response.status not in _REDIRECTS:
                    break
                if not response.location or redirect == MAX_REDIRECTS:
                    raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_redirect_limit")
                robots = _safe_robots_url(urljoin(robots.url, response.location))
                if robots.origin != current.origin:
                    raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_redirect_scope")
            if response.status in {404, 410}:
                allowed = True
            elif 200 <= response.status < 300:
                allowed = robots_allows(response.body, current.target)
            else:
                raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_unavailable")
            if not allowed:
                raise _RetrievalFailure(RetrievalStatus.BLOCKED, "robots_denied")
            remaining_requests = request.max_requests - usage.requests
            remaining_bytes = request.max_response_bytes - usage.transferred
            if remaining_requests <= 0 or remaining_bytes <= 0:
                raise _RetrievalFailure(
                    RetrievalStatus.BUDGET_EXHAUSTED, "robots_allowance_exhausted"
                )
            remaining_time = request.timeout_seconds - (asyncio.get_running_loop().time() - started)
            if remaining_time <= 0:
                raise TimeoutError
            result = await reader.retrieve(
                replace(
                    request,
                    max_requests=remaining_requests,
                    max_response_bytes=remaining_bytes,
                    timeout_seconds=remaining_time,
                ),
                allow_redirects=False,
            )
            return replace(
                result,
                usage=WebUsage(
                    page_attempts=1,
                    network_requests=usage.requests + result.usage.network_requests,
                    response_bytes=usage.transferred + result.usage.response_bytes,
                ),
            )
    except DestinationBlocked as error:
        code = str(error)
    except _RetrievalFailure as error:
        code = error.code if error.code.startswith("robots_") else "robots_unavailable"
        if error.status is RetrievalStatus.BUDGET_EXHAUSTED:
            failure_status = error.status
    except TimeoutError:
        code = "robots_timeout"
    except OSError, ValueError, ssl.SSLError, h11.RemoteProtocolError, zlib.error:
        code = "robots_unavailable"
    return RetrievalResult(
        failure_status,
        request.url,
        final_url=current.url if current else None,
        usage=WebUsage(
            page_attempts=1, network_requests=usage.requests, response_bytes=usage.transferred
        ),
        error_code=code,
    )
