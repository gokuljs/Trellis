"""Worker-only raw reader. No extraction, provider keys, cookies or environment proxies."""

import asyncio
import base64
import json
import ssl
import sys
import zlib
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urljoin

import h11

from app.domain.retrieval import (
    HTML_DECODED_BYTES,
    HTML_RAW_BYTES,
    MAX_REDIRECTS,
    MAX_WORKER_REQUEST_BYTES,
    PDF_RAW_BYTES,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
)
from app.domain.web import WebUsage
from app.infrastructure.web_network import (
    DestinationBlocked,
    PublicURL,
    parse_public_url,
    validate_scope,
)

_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_HEADERS = 32768


class _RetrievalFailure(Exception):
    def __init__(self, status: RetrievalStatus, code: str) -> None:
        self.status = status
        self.code = code


class _Decoder:
    def __init__(self, encoding: str, limit: int) -> None:
        self._limit = limit
        self._written = 0
        if encoding not in {"identity", "gzip", "deflate"}:
            raise _RetrievalFailure(RetrievalStatus.UNSUPPORTED, "unsupported_content_encoding")
        self._decoder = (
            None if encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
        )

    def decode(self, chunk: bytes) -> bytes:
        remaining = self._limit - self._written
        result = chunk if self._decoder is None else self._decoder.decompress(chunk, remaining + 1)
        self._written += len(result)
        if self._written > self._limit or (
            self._decoder is not None and self._decoder.unconsumed_tail
        ):
            raise _RetrievalFailure(RetrievalStatus.OVERSIZED, "decoded_document_limit")
        if self._decoder is not None and self._decoder.unused_data:
            raise _RetrievalFailure(RetrievalStatus.UNSUPPORTED, "trailing_compressed_data")
        return result

    def finish(self) -> None:
        # No flush: eof proves the trailer was consumed without unbounded expansion.
        if self._decoder is not None and not self._decoder.eof:
            raise _RetrievalFailure(RetrievalStatus.UNAVAILABLE, "incomplete_compressed_document")


class RawDocumentReader:
    def __init__(
        self,
        gateway_socket: Path,
        lease_token: str,
        *,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._gateway_socket = gateway_socket
        self._lease_token = lease_token
        self._tls_context = tls_context or ssl.create_default_context()

    async def _open(self, url: PublicURL) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_unix_connection(self._gateway_socket, limit=65536)
        try:
            writer.write(
                json.dumps(
                    {"lease_token": self._lease_token, "host": url.host, "port": url.port},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            await writer.drain()
            line = await reader.readline()
            if len(line) > 4096:
                raise ValueError("oversized gateway acknowledgement")
            reply = json.loads(line)
            if not isinstance(reply, dict):
                raise ValueError("invalid gateway acknowledgement")
            if reply.get("status") != "ok":
                status = RetrievalStatus(reply.get("status", "unavailable"))
                raise _RetrievalFailure(status, "gateway_" + status.value)
            if url.scheme == "https":
                await writer.start_tls(self._tls_context, server_hostname=url.host)
            return reader, writer
        except BaseException:
            await self._close(writer)
            raise

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except OSError, TimeoutError:
            pass

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        requests = 0
        transferred = 0
        current: PublicURL | None = None
        result_body = b""
        media_type: str | None = None
        status = RetrievalStatus.UNAVAILABLE
        error_code: str | None = None
        try:
            async with asyncio.timeout(request.timeout_seconds):
                current = parse_public_url(request.url)
                for redirect in range(MAX_REDIRECTS + 1):
                    validate_scope(current, request.scope_origin, request.scope_path_prefix)
                    if requests >= request.max_requests:
                        raise _RetrievalFailure(
                            RetrievalStatus.BUDGET_EXHAUSTED, "request_allowance_exhausted"
                        )
                    reader, writer = await self._open(current)
                    try:
                        connection = h11.Connection(
                            h11.CLIENT, max_incomplete_event_size=_MAX_HEADERS
                        )
                        writer.write(
                            connection.send(
                                h11.Request(
                                    method="GET",
                                    target=current.target,
                                    headers=[
                                        ("Host", current.authority),
                                        ("User-Agent", "TrellisReader/1.0"),
                                        ("Accept", "text/html,text/plain,application/pdf"),
                                        ("Accept-Encoding", "gzip, deflate"),
                                        ("Connection", "close"),
                                    ],
                                )
                            )
                        )
                        writer.write(connection.send(h11.EndOfMessage()))
                        requests += 1
                        await writer.drain()
                        response: h11.Response | None = None
                        decoder: _Decoder | None = None
                        body = bytearray()
                        raw_limit = HTML_RAW_BYTES
                        document_raw = 0
                        next_url: PublicURL | None = None
                        informational = 0
                        while True:
                            event = connection.next_event()
                            if event is h11.NEED_DATA:
                                available = request.max_response_bytes - transferred
                                if response is not None:
                                    available = min(available, raw_limit - document_raw)
                                if available <= 0:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.OVERSIZED, "raw_document_limit"
                                    )
                                data = await reader.read(min(16384, available))
                                connection.receive_data(data)
                                continue
                            if isinstance(event, h11.InformationalResponse):
                                informational += 1
                                if informational > 4 or event.status_code == 101:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.UNSUPPORTED, "unsupported_http_upgrade"
                                    )
                            elif isinstance(event, h11.Response):
                                response = event
                                headers: dict[str, str] = {}
                                for key, value in event.headers:
                                    name = key.decode("ascii").lower()
                                    if name in headers and name in {
                                        "location",
                                        "content-type",
                                        "content-encoding",
                                    }:
                                        raise ValueError("ambiguous response headers")
                                    headers[name] = value.decode("latin-1")
                                if sum(len(k) + len(v) for k, v in event.headers) > _MAX_HEADERS:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.OVERSIZED, "response_header_limit"
                                    )
                                if event.status_code in _REDIRECTS:
                                    location = headers.get("location", "")
                                    if redirect >= MAX_REDIRECTS or not location:
                                        raise _RetrievalFailure(
                                            RetrievalStatus.UNAVAILABLE, "redirect_limit"
                                        )
                                    next_url = parse_public_url(urljoin(current.url, location))
                                    validate_scope(
                                        next_url, request.scope_origin, request.scope_path_prefix
                                    )
                                    break
                                if event.status_code in {401, 403, 407, 451}:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.ACCESS_DENIED, "http_access_denied"
                                    )
                                if not 200 <= event.status_code < 300:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.UNAVAILABLE, "http_unsuccessful"
                                    )
                                media_type = (
                                    headers.get("content-type", "").split(";", 1)[0].strip().lower()
                                )
                                if media_type not in {"text/html", "text/plain", "application/pdf"}:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.UNSUPPORTED, "unsupported_media_type"
                                    )
                                raw_limit = (
                                    PDF_RAW_BYTES
                                    if media_type == "application/pdf"
                                    else HTML_RAW_BYTES
                                )
                                raw_limit = min(raw_limit, request.max_response_bytes - transferred)
                                length = headers.get("content-length")
                                if length is not None and int(length) > raw_limit:
                                    raise _RetrievalFailure(
                                        RetrievalStatus.OVERSIZED, "raw_document_limit"
                                    )
                                decoder = _Decoder(
                                    headers.get("content-encoding", "identity").strip().lower(),
                                    PDF_RAW_BYTES
                                    if media_type == "application/pdf"
                                    else HTML_DECODED_BYTES,
                                )
                            elif isinstance(event, h11.Data):
                                transferred += len(event.data)
                                document_raw += len(event.data)
                                if (
                                    document_raw > raw_limit
                                    or transferred > request.max_response_bytes
                                ):
                                    raise _RetrievalFailure(
                                        RetrievalStatus.OVERSIZED, "raw_document_limit"
                                    )
                                if decoder is None:
                                    raise ValueError("body before response")
                                body.extend(decoder.decode(event.data))
                            elif isinstance(event, h11.EndOfMessage):
                                if response is None or decoder is None:
                                    raise ValueError("missing response")
                                decoder.finish()
                                result_body = bytes(body)
                                status = RetrievalStatus.SUCCESS
                                break
                            elif isinstance(event, h11.ConnectionClosed):
                                raise _RetrievalFailure(
                                    RetrievalStatus.UNAVAILABLE, "incomplete_http_response"
                                )
                            else:
                                raise ValueError("unexpected HTTP event")
                        if next_url is None:
                            break
                        current = next_url
                    finally:
                        await self._close(writer)
        except DestinationBlocked as error:
            status, error_code = RetrievalStatus.BLOCKED, str(error)
        except _RetrievalFailure as error:
            status, error_code = error.status, error.code
        except TimeoutError:
            status, error_code = RetrievalStatus.TIMEOUT, "retrieval_deadline"
        except ssl.SSLError:
            status, error_code = RetrievalStatus.UNAVAILABLE, "tls_verification_failed"
        except OSError, ValueError, h11.RemoteProtocolError, zlib.error:
            status, error_code = RetrievalStatus.UNAVAILABLE, "invalid_or_unavailable_response"
        return RetrievalResult(
            status=status,
            requested_url=request.url,
            final_url=current.url if current else None,
            media_type=media_type,
            body=result_body,
            usage=WebUsage(page_attempts=1, network_requests=requests, response_bytes=transferred),
            error_code=error_code,
        )


def result_to_wire(result: RetrievalResult) -> dict[str, object]:
    return {**asdict(result), "body": base64.b64encode(result.body).decode("ascii")}


async def _run(payload: object) -> RetrievalResult:
    if not isinstance(payload, dict) or set(payload) != {
        "url",
        "max_requests",
        "max_response_bytes",
        "timeout_seconds",
        "scope_origin",
        "scope_path_prefix",
        "gateway_socket",
        "lease_token",
    }:
        raise ValueError("invalid worker request fields")
    token, socket = payload.pop("lease_token"), payload.pop("gateway_socket")
    if (
        not isinstance(token, str)
        or not isinstance(socket, str)
        or socket != "/run/trellis-egress/gateway.sock"
    ):
        raise ValueError("invalid worker gateway")
    request = RetrievalRequest(**payload)
    return await RawDocumentReader(Path(socket), token).retrieve(request)


def main() -> None:
    # The launcher bounds stdin; this independent cap rejects a mismatched caller.
    raw = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
    if len(raw) > MAX_WORKER_REQUEST_BYTES:
        raise ValueError("worker request exceeds limit")
    result = asyncio.run(_run(json.loads(raw)))
    sys.stdout.write(json.dumps(result_to_wire(result), separators=(",", ":")))


if __name__ == "__main__":
    main()
