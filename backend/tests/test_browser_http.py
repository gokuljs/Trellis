import asyncio
import gzip
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_web_reader import _fixture, _FixtureConnector, _response

from app.domain.retrieval import HTML_RAW_BYTES, RenderRequest, RetrievalStatus
from app.infrastructure.browser_http import BrowserHTTPClient
from app.infrastructure.web_gateway import PublicGateway
from app.infrastructure.web_network import DestinationBlocked
from app.infrastructure.web_reader import _RetrievalFailure


def _client(socket, token, **changes):
    client = BrowserHTTPClient(socket, token)
    client.configure(replace(RenderRequest("http://fixture.test/docs"), **changes))
    return client


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://user:pass@fixture.test/",
        "http://elsewhere.test/docs",
        "http://fixture.test/admin",
    ],
)
def test_main_redirect_rejects_destination_before_target_connection(tmp_path, target):
    async def exercise():
        response = _response(b"", f"Location: {target}\r\n".encode(), b"302 Found")
        async with _fixture(tmp_path, response) as (gateway, socket, connector, seen):
            client = _client(
                socket,
                gateway.issue(2, 75, 100000),
                scope_origin="http://fixture.test",
                scope_path_prefix="/docs",
            )
            with pytest.raises(DestinationBlocked):
                await client.document()
            assert connector.attempts == [("fixture.test", 80)]
            assert len(seen) == client.requests == 1

    asyncio.run(exercise())


def test_main_redirect_count_and_subresource_redirect_are_bounded(tmp_path):
    async def exercise():
        response = _response(b"", b"Location: /docs/again\r\n", b"302 Found")
        async with _fixture(tmp_path, response) as (gateway, socket, connector, seen):
            client = _client(socket, gateway.issue(2, 75, 100000))
            with pytest.raises(_RetrievalFailure) as failure:
                await client.document()
            assert failure.value.code == "redirect_limit"
            assert len(connector.attempts) == len(seen) == client.requests == 6
        async with _fixture(tmp_path, response) as (gateway, socket, connector, seen):
            client = _client(socket, gateway.issue(2, 75, 100000))
            result = await client.fetch("http://fixture.test/script.js")
            assert result.status == 302
            assert result.location == "/docs/again"
            assert result.body == b""
            assert len(connector.attempts) == len(seen) == client.requests == 1

    asyncio.run(exercise())


def test_browser_transport_allows_only_get_and_head_and_strips_response_credentials(tmp_path):
    async def exercise():
        response = _response(
            gzip.compress(b"supporting text"),
            b"Content-Encoding: gzip\r\nSet-Cookie: secret=value\r\n"
            b"Content-Security-Policy: default-src 'self'\r\n",
        )
        async with _fixture(tmp_path, response) as (gateway, socket, connector, seen):
            client = _client(socket, gateway.issue(2, 75, 100000))
            for method in ("POST", "PUT", "DELETE", "OPTIONS", "get"):
                with pytest.raises(_RetrievalFailure) as failure:
                    await client.fetch(client.request.url, method)
                assert failure.value.code == "browser_method_blocked"
            assert not connector.attempts
            result = await client.fetch(client.request.url)
            assert result.body == b"supporting text"
            assert result.media_type == "text/html"
            headers = dict(result.headers)
            assert headers["content-security-policy"] == "default-src 'self'"
            assert not {"content-encoding", "content-length", "set-cookie"}.intersection(headers)
            assert seen[0].startswith(b"GET /docs HTTP/1.1\r\n")
            assert b"Authorization" not in seen[0] and b"Cookie" not in seen[0]
        async with _fixture(
            tmp_path, _response(b"", b"Content-Length: 100\r\nContent-Encoding: gzip\r\n")
        ) as (gateway, socket, _, seen):
            client = _client(socket, gateway.issue(2, 75, 100000))
            assert (await client.fetch(client.request.url, "HEAD")).body == b""
            assert seen[0].startswith(b"HEAD /docs HTTP/1.1\r\n")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "headers", "code"),
    [
        (b"x" * (HTML_RAW_BYTES + 1), b"", "raw_document_limit"),
        (
            gzip.compress(b"x" * (4 * 1024 * 1024 + 1)),
            b"Content-Encoding: gzip\r\n",
            "decoded_document_limit",
        ),
        (b"", f"Content-Length: {HTML_RAW_BYTES + 1}\r\n".encode(), "raw_document_limit"),
    ],
    ids=["raw-body", "gzip-bomb", "declared-body"],
)
def test_renderer_rejects_raw_and_decoded_resource_overflow(tmp_path, body, headers, code):
    async def exercise():
        async with _fixture(tmp_path, _response(body, headers)) as (gateway, socket, _, _):
            client = _client(socket, gateway.issue(2, 75, 20000000))
            with pytest.raises(_RetrievalFailure) as failure:
                await client.fetch(client.request.url)
            assert failure.value.status is RetrievalStatus.OVERSIZED
            assert failure.value.code == code

    asyncio.run(exercise())


def test_browser_request_limit_has_two_active_transactions_and_no_hidden_allowance(tmp_path):
    async def exercise():
        observed = []
        active = 0
        peak = 0
        first_pair = asyncio.Event()
        release = asyncio.Event()

        async def handle(reader, writer):
            nonlocal active, peak
            try:
                observed.append(await reader.readuntil(b"\r\n\r\n"))
                active += 1
                peak = max(peak, active)
                if len(observed) == 2:
                    first_pair.set()
                await release.wait()
                writer.write(_response(b"ok"))
                await writer.drain()
            finally:
                active -= 1
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        connector = _FixtureConnector(server.sockets[0].getsockname()[1])
        gateway = PublicGateway("a" * 40, connector)
        socket = Path(f"/tmp/trellis-browser-test-{uuid4().hex}.sock")
        tunnels = await asyncio.start_unix_server(gateway.handle_tunnel, socket)
        pending = []
        try:
            token = gateway.issue(10, 75, 1000000)
            client = _client(socket, token)
            pending = [
                asyncio.create_task(client.fetch(f"http://fixture.test/item/{index}"))
                for index in range(80)
            ]
            async with asyncio.timeout(2):
                await first_pair.wait()
            assert len(observed) == 2
            assert client.requests == 75
            release.set()
            async with asyncio.timeout(5):
                outcomes = await asyncio.gather(*pending, return_exceptions=True)
            failures = [value for value in outcomes if isinstance(value, _RetrievalFailure)]
            assert len(failures) == 5
            assert all(value.code == "browser_request_allowance_exhausted" for value in failures)
            assert len(observed) == len(connector.attempts) == client.requests == 75
            assert peak == 2
            receipt = await gateway.revoke(token)
            assert receipt is not None
            assert receipt["tunnels"] == 75
        finally:
            release.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            tunnels.close()
            server.close()
            await gateway.close()
            await tunnels.wait_closed()
            await server.wait_closed()

    asyncio.run(exercise())


def test_cancelled_browser_http_closes_live_gateway_sockets(tmp_path):
    async def exercise():
        async with _fixture(tmp_path, b"", delay=10) as (gateway, socket, _, _):
            token = gateway.issue(2, 75, 100000)
            client = _client(socket, token)
            opened = asyncio.Event()
            original_open = client._open

            async def signal_open(url):
                connection = await original_open(url)
                opened.set()
                return connection

            client._open = signal_open
            task = asyncio.create_task(client.fetch(client.request.url))
            async with asyncio.timeout(2):
                await opened.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(2):
                await asyncio.gather(*tuple(gateway._handlers))
            assert not gateway._leases[token].writers
            assert not gateway._handlers
            assert client.requests == 1

    asyncio.run(exercise())
