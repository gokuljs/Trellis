"""Trusted, bounded HTTP for the isolated renderer; Chromium never owns egress."""

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin

import h11

from app.domain.retrieval import (
    HTML_DECODED_BYTES,
    HTML_RAW_BYTES,
    MAX_REDIRECTS,
    RenderRequest,
    RetrievalStatus,
)
from app.infrastructure.web_network import PublicURL, parse_public_url, validate_scope
from app.infrastructure.web_reader import RawDocumentReader, _Decoder, _RetrievalFailure

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_STRIP_HEADERS = frozenset(
    {
        "connection",
        "transfer-encoding",
        "content-encoding",
        "content-length",
        "set-cookie",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "trailer",
        "upgrade",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserHTTPResponse:
    url: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def media_type(self) -> str:
        return dict(self.headers).get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def location(self) -> str | None:
        return dict(self.headers).get("location") if self.status in _REDIRECTS else None


class BrowserHTTPClient(RawDocumentReader):
    def configure(self, request: RenderRequest) -> None:
        self.request = request
        self.requests = 0
        self.response_bytes = 0
        self.decoded_bytes = 0
        self._slots = asyncio.Semaphore(2)

    def destination(self, value: str) -> PublicURL:
        url = parse_public_url(value)
        validate_scope(url, self.request.scope_origin, self.request.scope_path_prefix)
        return url

    async def document(self) -> BrowserHTTPResponse:
        url = self.destination(self.request.url)
        for redirect in range(MAX_REDIRECTS + 1):
            response = await self.fetch(url.url)
            if response.status not in _REDIRECTS:
                return response
            if redirect == MAX_REDIRECTS or not response.location:
                raise _RetrievalFailure(RetrievalStatus.BLOCKED, "redirect_limit")
            url = self.destination(urljoin(url.url, response.location))
        raise AssertionError("bounded redirect loop")

    async def fetch(self, value: str, method: str = "GET") -> BrowserHTTPResponse:
        url = self.destination(value)
        if method not in {"GET", "HEAD"}:
            raise _RetrievalFailure(RetrievalStatus.BLOCKED, "browser_method_blocked")
        if self.requests >= self.request.max_requests:
            raise _RetrievalFailure(
                RetrievalStatus.BUDGET_EXHAUSTED, "browser_request_allowance_exhausted"
            )
        self.requests += 1
        async with self._slots:
            reader, writer = await self._open(url)
            try:
                connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=32768)
                writer.write(
                    connection.send(
                        h11.Request(
                            method=method,
                            target=url.target,
                            headers=[
                                ("Host", url.authority),
                                ("User-Agent", "TrellisRenderer/1.0"),
                                ("Accept", "*/*"),
                                ("Accept-Encoding", "gzip, deflate"),
                                ("Connection", "close"),
                            ],
                        )
                    )
                )
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                headers: dict[str, str] = {}
                status = 0
                decoder: _Decoder | None = None
                raw_bytes = 0
                body = bytearray()
                informational = 0
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        connection.receive_data(await reader.read(16384))
                    elif isinstance(event, h11.InformationalResponse):
                        informational += 1
                        if informational > 4 or event.status_code == 101:
                            raise ValueError("unsupported HTTP upgrade")
                    elif isinstance(event, h11.Response):
                        if sum(len(k) + len(v) for k, v in event.headers) > 32768:
                            raise ValueError("response header limit")
                        status = event.status_code
                        for key, header_value in event.headers:
                            name = key.decode("ascii").lower()
                            if name == "set-cookie":
                                continue
                            if name in headers:
                                if name in {
                                    "content-type",
                                    "content-encoding",
                                    "content-length",
                                    "location",
                                }:
                                    raise ValueError("ambiguous response headers")
                                headers[name] += ", " + header_value.decode("latin-1")
                            else:
                                headers[name] = header_value.decode("latin-1")
                        if status in _REDIRECTS:
                            if location := headers.get("location"):
                                self.destination(urljoin(url.url, location))
                            return BrowserHTTPResponse(url.url, status, tuple(headers.items()), b"")
                        if status in {401, 403, 407, 451}:
                            raise _RetrievalFailure(
                                RetrievalStatus.ACCESS_DENIED, "http_access_denied"
                            )
                        if not 200 <= status < 300:
                            raise _RetrievalFailure(
                                RetrievalStatus.UNAVAILABLE, "http_unsuccessful"
                            )
                        if "attachment" in headers.get("content-disposition", "").lower():
                            raise _RetrievalFailure(
                                RetrievalStatus.BLOCKED, "browser_download_blocked"
                            )
                        if int(headers.get("content-length", "0")) > HTML_RAW_BYTES:
                            raise _RetrievalFailure(RetrievalStatus.OVERSIZED, "raw_document_limit")
                        decoder = _Decoder(
                            headers.get("content-encoding", "identity").lower(), HTML_DECODED_BYTES
                        )
                    elif isinstance(event, h11.Data):
                        raw_bytes += len(event.data)
                        self.response_bytes += len(event.data)
                        if (
                            raw_bytes > HTML_RAW_BYTES
                            or self.response_bytes > self.request.max_response_bytes
                        ):
                            raise _RetrievalFailure(RetrievalStatus.OVERSIZED, "raw_document_limit")
                        if decoder is None:
                            raise ValueError("body before response")
                        chunk = decoder.decode(event.data)
                        self.decoded_bytes += len(chunk)
                        if self.decoded_bytes > 32 * 1024 * 1024:
                            raise _RetrievalFailure(
                                RetrievalStatus.OVERSIZED, "browser_decoded_allowance_exhausted"
                            )
                        body.extend(chunk)
                    elif isinstance(event, h11.EndOfMessage):
                        if decoder is None:
                            raise ValueError("missing response")
                        if method != "HEAD":
                            decoder.finish()
                        accepted = tuple(
                            (key, value)
                            for key, value in headers.items()
                            if key not in _STRIP_HEADERS
                        )
                        return BrowserHTTPResponse(url.url, status, accepted, bytes(body))
                    elif isinstance(event, h11.ConnectionClosed):
                        raise ValueError("incomplete HTTP response")
                    else:
                        raise ValueError("unexpected HTTP event")
            finally:
                await self._close(writer)
