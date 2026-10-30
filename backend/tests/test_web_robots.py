import asyncio
import gzip
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.retrieval import RetrievalRequest, RetrievalStatus
from app.infrastructure.web_gateway import PublicGateway
from app.infrastructure.web_network import DestinationBlocked
from app.infrastructure.web_reader import RawDocumentReader
from app.infrastructure.web_robots import (
    ROBOTS_MAX_BYTES,
    retrieve_crawl_document,
    robots_allows,
)


@pytest.mark.parametrize(
    ("target", "allowed"),
    [("/private", False), ("/private/public", True), ("/secret", False), ("/other", True)],
)
def test_combines_matching_groups_and_ignores_wildcard_fallback(target, allowed):
    body = b"""User-agent: *
Disallow: /
User-agent: TrellisReader
Disallow: /private
Allow: /private/public
User-agent: OtherBot
Disallow: /other
User-agent: trellisreader
Disallow: /secret
"""
    assert robots_allows(body, target) is allowed


@pytest.mark.parametrize(
    ("rules", "target", "allowed"),
    [
        ("Disallow: /private", "/Private", True),
        ("Disallow: /private\nAllow: /private", "/private", True),
        ("Allow: /page\nDisallow: /*.htm", "/page.htm", False),
        ("Allow: /a\nDisallow: /a$", "/a", False),
        ("Allow: /page\nDisallow: /*.ph", "/page.php5", True),
        ("Allow: /fish\nDisallow: /fish*", "/fish.html", False),
        ("Allow: /fish*\nDisallow: /fish", "/fish.html", True),
        ("Allow: /bar\nDisallow: /%62ar", "/bar", True),
        ("Allow: /ツ\nDisallow: /%E3%83%84", "/%E3%83%84", True),
        ("Allow: /ツ\nDisallow: /%E3%83%84$", "/%E3%83%84", False),
        ("Disallow: /*.pdf$", "/paper.pdf", False),
        ("Disallow: *.pdf$", "/paper.pdf", False),
        ("Disallow: /*.pdf$", "/paper.pdf?download=1", True),
        ("Disallow: /docs/*/draft$", "/docs/a/b/draft", False),
        ("Disallow: /docs/*/draft$", "/docs/a/draft/next", True),
        ("Disallow: /docs?token=", "/docs?token=x", False),
        ("Disallow: /docs?token=", "/docs", True),
        ("Disallow: /ツ", "/%E3%83%84", False),
        ("Disallow: /%E3%83%84", "/ツ", False),
        ("Disallow: /%62%61%7a", "/baz", False),
        ("Disallow: /a%2Fb", "/a/b", True),
        ("Disallow: /a%2fb", "/a%2Fb", False),
        ("Disallow: /foo-%2A", "/foo-*", False),
        ("Disallow: /foo-%24", "/foo-$", False),
        ("Disallow: /foo-$bar", "/foo-$bar", False),
        ("Disallow: /a%23b # comment", "/a%23b", False),
        ("Disallow: /a#b", "/ab", False),
        ("Disallow: /%+1", "/%01", True),
        ("Disallow:\nAllow:", "/anything", True),
        ("Disallow: /", "/robots.txt", True),
    ],
)
def test_matches_rfc_paths_wildcards_queries_and_encoding(rules, target, allowed):
    assert robots_allows(("User-agent: *\n" + rules).encode(), target) is allowed


def test_ignores_ungrouped_unknown_and_invalid_lines_without_rewriting_lines():
    body = (
        b"Disallow: /unscoped\nUser-Agent: *\nDisallow: /private\n"
        b"Sitemap: https://elsewhere.test/sitemap.xml\nAllow: /private/open\n"
        b"garbage\nDisallow: \xff\n" + b"#" * 4096 + b"\nDisallow: /last\n"
    )
    assert robots_allows(body, "/unscoped")
    assert robots_allows(body, "/private/open")
    assert not robots_allows(body, "/last")
    assert robots_allows(b"User-agent: OtherBot\nDisallow: /", "/anything")
    assert not robots_allows(b"#" * (ROBOTS_MAX_BYTES + 1), "/anything")


def test_empty_matching_group_disables_wildcard_fallback():
    body = b"User-agent: *\nDisallow: /\nUser-agent: TrellisReader\n"
    assert robots_allows(body, "/private")


def test_many_wildcards_match_without_regex_backtracking():
    rule = "/" + "*a" * 1000 + "*b$"
    assert robots_allows(("User-agent: *\nDisallow: " + rule).encode(), "/" + "a" * 4095)


def test_unicode_line_separators_do_not_create_synthetic_rules():
    body = "User-agent: *\nUnknown: ignored\u0085Disallow: /private".encode()
    assert robots_allows(body, "/private")


def _response(body=b"", *, status=b"200 OK", headers=b""):
    return b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\n" + headers + b"\r\n" + body


@asynccontextmanager
async def _fixture(
    responses: dict[str, bytes], *, delay: float = 0, observed: asyncio.Event | None = None
) -> AsyncIterator[tuple[PublicGateway, Path, list[str]]]:
    seen: list[str] = []
    handlers: set[asyncio.Task] = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            target = request.split(b" ", 2)[1].decode()
            seen.append(target)
            if observed is not None:
                observed.set()
            assert b"User-Agent: TrellisReader/1.0\r\n" in request
            assert b"Cookie:" not in request and b"Authorization:" not in request
            if delay:
                await asyncio.sleep(delay)
            response = responses.get(target, _response(status=b"404 Not Found"))
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

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    fixture_port = server.sockets[0].getsockname()[1]

    class Connector:
        async def connect(self, host, port):
            if host != "fixture.test" or port != 80:
                raise DestinationBlocked("fixture_destination_blocked")
            return await asyncio.open_connection("127.0.0.1", fixture_port)

    gateway = PublicGateway("a" * 40, Connector())
    socket = Path(f"/tmp/trellis-robots-{uuid4().hex}.sock")
    tunnels = await asyncio.start_unix_server(gateway.handle_tunnel, socket, limit=16384)
    try:
        yield gateway, socket, seen
    finally:
        tunnels.close()
        server.close()
        await gateway.close()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await tunnels.wait_closed()
        await server.wait_closed()


def _request(**changes):
    return RetrievalRequest(
        url="http://fixture.test/docs/page",
        max_requests=7,
        max_response_bytes=18 * 1024 * 1024,
        scope_origin="http://fixture.test",
        scope_path_prefix="/docs",
        **changes,
    )


@pytest.mark.parametrize("status", [b"404 Not Found", b"410 Gone"])
def test_missing_robots_allows_page_and_charges_both_requests(status):
    async def run():
        responses = {
            "/robots.txt": _response(status=status),
            "/docs/page": _response(b"Public evidence."),
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.status is RetrievalStatus.SUCCESS
            assert result.body == b"Public evidence."
            assert result.usage.page_attempts == 1
            assert result.usage.network_requests == 2
            assert result.usage.response_bytes == len(result.body)
            assert seen == ["/robots.txt", "/docs/page"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        _response(status=b"401 Unauthorized"),
        _response(status=b"403 Forbidden"),
        _response(status=b"429 Too Many Requests"),
        _response(status=b"500 Server Error"),
        _response(status=b"418 Teapot"),
        _response(b"User-agent: *\nDisallow: /docs"),
        _response(b"x" * (ROBOTS_MAX_BYTES + 1)),
        _response(
            gzip.compress(b"x" * (ROBOTS_MAX_BYTES + 1)), headers=b"Content-Encoding: gzip\r\n"
        ),
        _response(b"short", headers=b"Content-Length: 100\r\n"),
        b"not HTTP",
    ],
    ids=[
        "401",
        "403",
        "429",
        "500",
        "418",
        "denied",
        "raw-limit",
        "gzip-limit",
        "truncated",
        "invalid",
    ],
)
def test_robots_failures_deny_without_requesting_the_page(response):
    async def run():
        async with _fixture({"/robots.txt": response}) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.status is RetrievalStatus.BLOCKED
            assert result.body == b""
            assert result.error_code is not None
            assert result.error_code.startswith("robots_")
            assert result.usage.network_requests == 1
            assert seen == ["/robots.txt"]

    asyncio.run(run())


def test_same_origin_robots_redirect_and_gzip_rules_are_applied():
    async def run():
        responses = {
            "/robots.txt": _response(status=b"302 Found", headers=b"Location: /policy.txt\r\n"),
            "/policy.txt": _response(
                gzip.compress(b"User-agent: *\nDisallow: /docs"),
                headers=b"Content-Encoding: gzip\r\n",
            ),
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.error_code == "robots_denied"
            assert result.usage.network_requests == 2
            assert seen == ["/robots.txt", "/policy.txt"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "location",
    [
        "http://elsewhere.test/robots.txt",
        "http://127.0.0.1/robots.txt",
        "https://fixture.test/robots.txt",
    ],
)
def test_robots_redirect_cannot_change_origin_or_reach_private_targets(location):
    async def run():
        responses = {
            "/robots.txt": _response(
                status=b"302 Found", headers=f"Location: {location}\r\n".encode()
            )
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.status is RetrievalStatus.BLOCKED
            assert seen == ["/robots.txt"]

    asyncio.run(run())


def test_robots_redirects_have_a_hard_five_redirect_limit():
    async def run():
        responses = {
            "/robots.txt": _response(status=b"302 Found", headers=b"Location: /robots.txt\r\n")
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.error_code == "robots_redirect_limit"
            assert len(seen) == 6
            assert result.usage.network_requests == 6

    asyncio.run(run())


@pytest.mark.parametrize(
    "location",
    [
        "/policy?token=private",
        "/policy?%2574oken%253Dprivate",
        "/policy?token+=private",
        "/policy?x=Bearer+abcdef123456",
    ],
)
def test_robots_redirect_never_fetches_secret_bearing_urls(location):
    async def run():
        responses = {
            "/robots.txt": _response(
                status=b"302 Found", headers=f"Location: {location}\r\n".encode()
            )
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.error_code == "robots_sensitive_url"
            assert seen == ["/robots.txt"]

    asyncio.run(run())


def test_page_redirect_is_blocked_before_a_robots_denied_destination_is_requested():
    async def run():
        responses = {
            "/robots.txt": _response(b"User-agent: *\nDisallow: /docs/private"),
            "/docs/page": _response(status=b"302 Found", headers=b"Location: /docs/private\r\n"),
        }
        async with _fixture(responses) as (gateway, socket, seen):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
            )
            assert result.error_code == "crawl_redirect_blocked"
            assert result.status is RetrievalStatus.BLOCKED
            assert result.usage.network_requests == 2
            assert seen == ["/robots.txt", "/docs/page"]

    asyncio.run(run())


def test_page_scope_is_checked_before_robots_or_page_network_requests():
    async def run():
        async with _fixture({}) as (gateway, socket, seen):
            request = RetrievalRequest(
                "http://fixture.test/private",
                scope_origin="http://fixture.test",
                scope_path_prefix="/docs",
            )
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), request
            )
            assert result.error_code == "outside_navigation_scope"
            assert result.usage.network_requests == 0
            assert not seen

    asyncio.run(run())


def test_robots_cannot_spend_the_last_request_and_then_fetch_a_page():
    async def run():
        async with _fixture({"/robots.txt": _response(status=b"404 Not Found")}) as (
            gateway,
            socket,
            seen,
        ):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 1, 20000000)),
                RetrievalRequest("http://fixture.test/docs/page", max_requests=1),
            )
            assert result.error_code == "robots_allowance_exhausted"
            assert result.status is RetrievalStatus.BUDGET_EXHAUSTED
            assert result.usage.network_requests == 1
            assert seen == ["/robots.txt"]

    asyncio.run(run())


def test_robots_deadline_and_cancellation_close_worker_connections():
    async def run():
        observed = asyncio.Event()
        async with _fixture({}, delay=10, observed=observed) as (gateway, socket, _):
            result = await retrieve_crawl_document(
                RawDocumentReader(socket, gateway.issue(2, 7, 20000000)),
                _request(timeout_seconds=0.02),
            )
            assert result.error_code == "robots_timeout"
            observed.clear()
            task = asyncio.create_task(
                retrieve_crawl_document(
                    RawDocumentReader(socket, gateway.issue(2, 7, 20000000)), _request()
                )
            )
            await observed.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await gateway.close()
            assert not gateway._leases

    asyncio.run(run())
