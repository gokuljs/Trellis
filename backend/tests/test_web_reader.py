import asyncio
import gzip
import json
import ssl
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.domain.retrieval import HTML_RAW_BYTES, RetrievalRequest, RetrievalStatus
from app.infrastructure.web_gateway import PublicGateway
from app.infrastructure.web_network import DestinationBlocked
from app.infrastructure.web_reader import RawDocumentReader

_SECRET = "a" * 40


class _FixtureConnector:
    def __init__(self, port: int) -> None:
        self.port = port
        self.attempts: list[tuple[str, int]] = []

    async def connect(self, host: str, port: int):
        self.attempts.append((host, port))
        if host != "fixture.test":
            raise DestinationBlocked("fixture_destination_blocked")
        return await asyncio.open_connection("127.0.0.1", self.port)


@asynccontextmanager
async def _fixture(
    tmp_path: Path,
    response: bytes,
    *,
    tls: ssl.SSLContext | None = None,
    delay: float = 0,
) -> AsyncIterator[tuple[PublicGateway, Path, _FixtureConnector, list[bytes]]]:
    observed: list[bytes] = []
    handlers: set[asyncio.Task] = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            observed.append(await reader.readuntil(b"\r\n\r\n"))
            if delay:
                await asyncio.sleep(delay)
            for offset in range(0, len(response), 8192):
                writer.write(response[offset : offset + 8192])
                await writer.drain()
        except OSError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    connector = _FixtureConnector(server.sockets[0].getsockname()[1])
    gateway = PublicGateway(_SECRET, connector)
    socket = Path(f"/tmp/trellis-test-{uuid4().hex}.sock")
    tunnels = await asyncio.start_unix_server(gateway.handle_tunnel, socket, limit=16384)
    try:
        yield gateway, socket, connector, observed
    finally:
        tunnels.close()
        server.close()
        await gateway.close()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await tunnels.wait_closed()
        await server.wait_closed()


def _response(body: bytes, headers: bytes = b"", status: bytes = b"200 OK") -> bytes:
    return b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html\r\n" + headers + b"\r\n" + body


def test_reads_close_delimited_html_without_environment_proxies_or_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://secret:password@127.0.0.1:1")

    async def run():
        async with _fixture(tmp_path, _response(b"hello")) as (gateway, socket, connector, seen):
            token = gateway.issue(2, 6, 100000)
            result = await RawDocumentReader(socket, token).retrieve(
                RetrievalRequest("http://fixture.test/")
            )
            assert result.status is RetrievalStatus.SUCCESS
            assert result.body == b"hello"
            assert result.usage.network_requests == 1
            assert result.usage.response_bytes == 5
            assert connector.attempts == [("fixture.test", 80)]
            assert b"Host: fixture.test\r\n" in seen[0]
            assert b"Authorization" not in seen[0] and b"Cookie" not in seen[0]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (_response(b"x" * (HTML_RAW_BYTES + 1)), RetrievalStatus.OVERSIZED, "raw_document_limit"),
        (
            _response(gzip.compress(b"x" * (4 * 1024 * 1024 + 1)), b"Content-Encoding: gzip\r\n"),
            RetrievalStatus.OVERSIZED,
            "decoded_document_limit",
        ),
        (
            _response(gzip.compress(b"hello")[:-2], b"Content-Encoding: gzip\r\n"),
            RetrievalStatus.UNAVAILABLE,
            "incomplete_compressed_document",
        ),
        (
            _response(
                gzip.compress(b"hello") + gzip.compress(b"world"), b"Content-Encoding: gzip\r\n"
            ),
            RetrievalStatus.UNSUPPORTED,
            "trailing_compressed_data",
        ),
        (
            _response(b"hello", b"Content-Encoding: br\r\n"),
            RetrievalStatus.UNSUPPORTED,
            "unsupported_content_encoding",
        ),
        (
            _response(b"denied", status=b"403 Forbidden"),
            RetrievalStatus.ACCESS_DENIED,
            "http_access_denied",
        ),
        (
            _response(b"short", b"Content-Length: 100\r\n"),
            RetrievalStatus.UNAVAILABLE,
            "invalid_or_unavailable_response",
        ),
    ],
    ids=[
        "raw-overflow",
        "compression-bomb",
        "incomplete-gzip",
        "gzip-multiple-members",
        "unsupported-encoding",
        "http-denied",
        "truncated-response",
    ],
)
def test_bounds_streams_decompression_and_http_failures(tmp_path, response, status, code):
    async def run():
        async with _fixture(tmp_path, response) as (gateway, socket, _, _):
            result = await RawDocumentReader(socket, gateway.issue(2, 6, 20000000)).retrieve(
                RetrievalRequest("http://fixture.test/")
            )
            assert result.status is status
            assert result.error_code == code
            assert result.body == b""

    asyncio.run(run())


def test_gateway_transport_budget_is_authoritative_idempotent_and_never_exceeded(tmp_path):
    async def run():
        async with _fixture(tmp_path, _response(b"x" * 1000)) as (gateway, socket, _, _):
            token = gateway.issue(2, 1, 100)
            reader, writer = await asyncio.open_unix_connection(socket)
            writer.write(
                json.dumps({"lease_token": token, "host": "fixture.test", "port": 80}).encode()
                + b"\n"
            )
            await writer.drain()
            assert json.loads(await reader.readline()) == {"status": "ok"}
            request = b"GET / HTTP/1.1\r\nHost: fixture.test\r\n\r\n"
            writer.write(request)
            await writer.drain()
            received = await reader.read()
            receipt = await gateway.revoke(token)
            assert receipt == {"tunnels": 1, "transferred_bytes": 100}
            assert len(request) + len(received) == 100
            assert await gateway.revoke(token) == receipt
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


def test_retired_lease_cannot_forward_queued_bytes_or_change_its_receipt():
    async def run():
        remote_reader = asyncio.StreamReader()

        class RemoteWriter:
            def __init__(self):
                self.writes = []

            def write(self, value):
                self.writes.append(value)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        remote_writer = RemoteWriter()

        class Connector:
            async def connect(self, host, port):
                return remote_reader, remote_writer

        gateway = PublicGateway(_SECRET, Connector())
        socket = Path(f"/tmp/trellis-test-{uuid4().hex}.sock")
        server = await asyncio.start_unix_server(gateway.handle_tunnel, socket)
        token = gateway.issue(2, 1, 1000)
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
            writer.write(
                json.dumps({"lease_token": token, "host": "fixture.test", "port": 80}).encode()
                + b"\n"
            )
            await writer.drain()
            assert json.loads(await reader.readline()) == {"status": "ok"}
            await asyncio.sleep(0)
            lease = gateway._leases[token]
            handlers = tuple(gateway._handlers)
            # Put the pump on the ready queue before its owner receives cancellation.
            remote_reader.feed_data(b"queued-after-retirement")
            gateway._expire(token)
            receipt = await gateway.revoke(token)
            await asyncio.gather(*handlers, return_exceptions=True)
            assert lease.transferred == 0
            assert receipt == {"tunnels": 1, "transferred_bytes": 0}
            assert await gateway.revoke(token) == receipt
            assert await reader.read() == b""
            assert not remote_writer.writes
            writer.close()
            await writer.wait_closed()
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            server.close()
            await gateway.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::ffff:8.8.8.8]/",
        "http://user:pass@fixture.test/",
        "http://fixture.test/admin",
        "http://elsewhere.test/docs/page",
    ],
)
def test_redirects_are_revalidated_before_target_connection(tmp_path, target):
    async def run():
        raw = _response(b"", f"Location: {target}\r\n".encode(), b"302 Found")
        async with _fixture(tmp_path, raw) as (gateway, socket, connector, _):
            result = await RawDocumentReader(socket, gateway.issue(2, 6, 100000)).retrieve(
                RetrievalRequest(
                    "http://fixture.test/docs",
                    scope_origin="http://fixture.test",
                    scope_path_prefix="/docs",
                )
            )
            assert result.status is RetrievalStatus.BLOCKED
            assert len(connector.attempts) == 1

    asyncio.run(run())


def test_redirect_limit_and_request_allowance_are_bounded(tmp_path):
    async def run():
        raw = _response(b"", b"Location: /again\r\n", b"302 Found")
        async with _fixture(tmp_path, raw) as (gateway, socket, connector, _):
            result = await RawDocumentReader(socket, gateway.issue(2, 6, 100000)).retrieve(
                RetrievalRequest("http://fixture.test/")
            )
            assert result.error_code == "redirect_limit"
            assert len(connector.attempts) == 6
            result = await RawDocumentReader(socket, gateway.issue(2, 6, 100000)).retrieve(
                RetrievalRequest("http://fixture.test/", max_requests=1)
            )
            assert result.status is RetrievalStatus.BUDGET_EXHAUSTED
            assert result.usage.network_requests == 1

    asyncio.run(run())


def test_gateway_revocation_and_expiry_close_live_sockets(tmp_path):
    async def run():
        async with _fixture(tmp_path, b"", delay=10) as (gateway, socket, _, _):
            for revoke in (True, False):
                token = gateway.issue(2 if revoke else 0.05, 1, 100000)
                reader, writer = await asyncio.open_unix_connection(socket)
                writer.write(
                    json.dumps({"lease_token": token, "host": "fixture.test", "port": 80}).encode()
                    + b"\n"
                )
                await writer.drain()
                assert json.loads(await reader.readline()) == {"status": "ok"}
                if revoke:
                    await gateway.revoke(token)
                async with asyncio.timeout(1):
                    assert await reader.read() == b""
                writer.close()
                await writer.wait_closed()

    asyncio.run(run())


def test_reader_deadline_and_cancellation_release_gateway_sockets(tmp_path):
    async def run():
        async with _fixture(tmp_path, b"", delay=10) as (gateway, socket, _, _):
            result = await RawDocumentReader(socket, gateway.issue(2, 1, 100000)).retrieve(
                RetrievalRequest("http://fixture.test/", timeout_seconds=0.05)
            )
            assert result.status is RetrievalStatus.TIMEOUT
            task = asyncio.create_task(
                RawDocumentReader(socket, gateway.issue(2, 1, 100000)).retrieve(
                    RetrievalRequest("http://fixture.test/")
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await gateway.close()
            assert not gateway._leases

    asyncio.run(run())


def test_admin_is_separate_and_authenticates_all_operations(tmp_path):
    async def run():
        gateway = PublicGateway(_SECRET)
        server = await asyncio.start_server(gateway.handle_admin, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                base = f"http://127.0.0.1:{port}"
                assert (await client.get(base + "/ready")).status_code == 403
                headers = {"Authorization": "Bearer " + _SECRET}
                assert (await client.get(base + "/ready", headers=headers)).json() == {
                    "policy": "public-http-v1"
                }
                lease = await client.post(
                    base + "/leases",
                    headers=headers,
                    json={"ttl_seconds": 1, "max_tunnels": 2, "max_bytes": 1000},
                )
                assert lease.status_code == 201
                token = lease.json()["token"]
                assert (await client.delete(base + "/leases/" + token, headers=headers)).json() == {
                    "revoked": True,
                    "receipt": {"tunnels": 0, "transferred_bytes": 0},
                }
                assert not gateway._leases
        finally:
            server.close()
            await server.wait_closed()
            await gateway.close()

    asyncio.run(run())


def test_tls_verifies_original_hostname_through_gateway(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=fixture.test",
            "-addext",
            "subjectAltName=DNS:fixture.test",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    sni = []
    server_tls.set_servername_callback(lambda _, host, __: sni.append(host))
    client_tls = ssl.create_default_context(cafile=str(cert))

    async def run():
        async with _fixture(tmp_path, _response(b"TLS"), tls=server_tls) as (
            gateway,
            socket,
            _,
            seen,
        ):
            result = await RawDocumentReader(
                socket, gateway.issue(2, 1, 100000), tls_context=client_tls
            ).retrieve(RetrievalRequest("https://fixture.test/"))
            assert result.status is RetrievalStatus.SUCCESS
            assert result.body == b"TLS"
            assert sni == ["fixture.test"]
            assert b"Host: fixture.test\r\n" in seen[0]

    asyncio.run(run())


def test_gateway_revoke_cancels_pending_connection_before_it_can_open(tmp_path):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class SlowConnector:
            async def connect(self, host, port):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        gateway = PublicGateway(_SECRET, SlowConnector())
        socket = Path(f"/tmp/trellis-test-{uuid4().hex}.sock")
        server = await asyncio.start_unix_server(gateway.handle_tunnel, socket)
        token = gateway.issue(2, 1, 10000)
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
            writer.write(
                json.dumps({"lease_token": token, "host": "fixture.test", "port": 80}).encode()
                + b"\n"
            )
            await writer.drain()
            await entered.wait()
            await gateway.revoke(token)
            assert cancelled.is_set()
            assert await reader.read() == b""
            assert not gateway._handlers
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            await gateway.close()

    asyncio.run(run())


def test_supported_plaintext_and_pdf_keep_separate_document_limits(tmp_path):
    async def run():
        for media, body in (
            (b"text/plain", b"User-agent: *\nDisallow: /admin"),
            (b"application/pdf", b"%PDF-" + b"x" * HTML_RAW_BYTES),
        ):
            raw = b"HTTP/1.1 200 OK\r\nContent-Type: " + media + b"\r\n\r\n" + body
            async with _fixture(tmp_path, raw) as (gateway, socket, _, _):
                result = await RawDocumentReader(socket, gateway.issue(2, 1, 20000000)).retrieve(
                    RetrievalRequest("http://fixture.test/")
                )
                assert result.status is RetrievalStatus.SUCCESS
                assert result.media_type == media.decode()
                assert result.body == body

    asyncio.run(run())
