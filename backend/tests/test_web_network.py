import asyncio
import socket
from unittest.mock import AsyncMock, Mock

import dns.asyncbackend
import dns.asyncresolver
import dns.message
import dns.rdatatype
import dns.rrset
import pytest

from app.infrastructure.web_network import (
    DestinationBlocked,
    PublicConnector,
    SystemResolver,
    parse_public_url,
    public_address,
    validate_scope,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.1.1.1",
        "172.16.1.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "168.63.129.16",
        "192.0.0.1",
        "192.0.0.9",
        "192.88.99.1",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:8.8.8.8",
        "::ffff:127.0.0.1",
        "::127.0.0.1",
        "::ffff:0:127.0.0.1",
        "64:ff9b::808:808",
        "64:ff9b:1::808:808",
        "2002:0808:0808::1",
        "2001::1",
        "2001:20::1",
        "2001:30::1",
    ],
)
def test_nonpublic_and_transition_addresses_are_rejected(address: str) -> None:
    assert not public_address(address)
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(DestinationBlocked):
        parse_public_url(f"http://{host}/")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://example.com:8080/",
        "http://example.com:/",
        "http://example.com./",
        "http://example.com\\@other.com/",
        "http://%31%32%37.0.0.1/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://[::1]/",
        "http://example.com/\r\nX:a",
        " http://example.com/",
        "http://example..com/",
        "http://[fe80::1%eth0]/",
    ],
)
def test_ambiguous_or_privileged_urls_are_rejected(url: str) -> None:
    with pytest.raises(DestinationBlocked):
        parse_public_url(url)


def test_urls_preserve_original_authority_and_escape_unicode_path() -> None:
    parsed = parse_public_url("https://EXAMPLE.com/café?q=hello#fragment")
    assert parsed.url == "https://example.com/caf%C3%A9?q=hello"
    assert parsed.authority == "example.com"
    assert parse_public_url("https://[2606:4700:4700::1111]/").host == "2606:4700:4700::1111"
    assert parse_public_url("http://example.com:443/").authority == "example.com:443"


@pytest.mark.parametrize(
    "target",
    [
        "https://elsewhere.test/docs/page",
        "https://example.test/docstring",
        "https://example.test/docs/%2e%2e/admin",
        "https://example.test/docs/%252e%252e/admin",
        "https://example.test/docs/a%5cb",
    ],
)
def test_navigation_scope_rejects_origin_and_encoded_path_escapes(target: str) -> None:
    with pytest.raises(DestinationBlocked):
        validate_scope(parse_public_url(target), "https://example.test", "/docs")


def test_scope_accepts_only_path_boundary() -> None:
    validate_scope(
        parse_public_url("https://example.test/docs/page"), "https://example.test", "/docs"
    )
    validate_scope(parse_public_url("https://example.test/docs"), "https://example.test", "/docs/")


@pytest.mark.parametrize(
    "answers",
    [
        (),
        ("127.0.0.1",),
        ("8.8.8.8", "10.0.0.1"),
        ("2606:4700:4700::1111", "::1"),
        ("8.8.8.8", "::ffff:8.8.8.8"),
        ("::127.0.0.1",),
        ("::ffff:0:127.0.0.1",),
    ],
)
def test_every_dns_answer_is_vetted_before_any_connection(monkeypatch, answers) -> None:
    resolver = AsyncMock()
    resolver.resolve.return_value = answers
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(DestinationBlocked if answers else OSError):
        asyncio.run(PublicConnector(resolver).connect("example.test", 443))
    connect.assert_not_awaited()


def test_connection_pins_dns_address_and_checks_actual_peer(monkeypatch) -> None:
    resolver = AsyncMock()
    resolver.resolve.return_value = ("8.8.8.8",)
    writer = AsyncMock()
    writer.close = Mock()
    writer.get_extra_info = lambda _: ("127.0.0.1", 443)
    connect = AsyncMock(return_value=(AsyncMock(), writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(DestinationBlocked):
        asyncio.run(PublicConnector(resolver).connect("example.test", 443))
    assert connect.await_args is not None
    assert connect.await_args.args[:2] == ("8.8.8.8", 443)
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_dns_is_revalidated_for_each_new_connection(monkeypatch) -> None:
    resolver = AsyncMock()
    resolver.resolve.side_effect = [("8.8.8.8",), ("127.0.0.1",)]
    writer = AsyncMock()
    writer.get_extra_info = lambda _: ("8.8.8.8", 443)
    connect = AsyncMock(return_value=(AsyncMock(), writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def run():
        connector = PublicConnector(resolver)
        await connector.connect("example.test", 443)
        with pytest.raises(DestinationBlocked):
            await connector.connect("example.test", 443)

    asyncio.run(run())
    assert connect.await_count == 1


def test_cancelled_dns_closes_async_sockets_without_saturating_blocking_resolver(monkeypatch):
    async def run():
        class DNSServer(asyncio.DatagramProtocol):
            def __init__(self):
                self.transport = None
                self.seen = 0
                self.respond = False
                self.queries_started = asyncio.Event()

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                self.seen += 1
                if self.seen >= 128:
                    self.queries_started.set()
                if not self.respond:
                    return
                query = dns.message.from_wire(data)
                response = dns.message.make_response(query)
                question = query.question[0]
                answer = "8.8.8.8" if question.rdtype == dns.rdatatype.A else "2606:4700::1111"
                response.answer.append(
                    dns.rrset.from_text(question.name, 60, "IN", question.rdtype, answer)
                )
                assert self.transport is not None
                self.transport.sendto(response.to_wire(), addr)

        transport, server = await asyncio.get_running_loop().create_datagram_endpoint(
            DNSServer, local_addr=("127.0.0.1", 0)
        )
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = transport.get_extra_info("sockname")[1]
        boundary = SystemResolver(resolver)
        backend = dns.asyncbackend.get_default_backend()
        original_make_socket = backend.make_socket
        opened = []

        async def observe_socket(*args, **kwargs):
            result = await original_make_socket(*args, **kwargs)
            opened.append(result.transport)
            return result

        monkeypatch.setattr(backend, "make_socket", observe_socket)
        forbidden = Mock(side_effect=AssertionError("blocking getaddrinfo must not run"))
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        tasks = [
            asyncio.create_task(boundary.resolve(f"cancel-{index}.test", 443))
            for index in range(64)
        ]
        try:
            async with asyncio.timeout(2):
                await server.queries_started.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert len(opened) >= 128
            assert all(connection.is_closing() for connection in opened)
            server.respond = True
            async with asyncio.timeout(1):
                assert set(await boundary.resolve("fresh.test", 443)) == {
                    "8.8.8.8",
                    "2606:4700::1111",
                }
            forbidden.assert_not_called()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            transport.close()
            await asyncio.sleep(0)

    asyncio.run(run())
