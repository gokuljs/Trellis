"""One fixed public Brave request with no application-authenticated client state."""

import asyncio
import hashlib
import json
from urllib.parse import urlsplit

import httpx

from app.application.tool_context import ToolExecutionContext
from app.domain.search import (
    MAX_SEARCH_QUERY_CHARS,
    MAX_SEARCH_RESPONSE_BYTES,
    MAX_SEARCH_RESULTS,
    SearchHit,
    SearchResult,
    SearchStatus,
)

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _reject_constant(_value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _hits(payload: bytes, maximum: int) -> tuple[SearchHit, ...]:
    value = json.loads(payload, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid search response")
    web = value.get("web")
    if web is None:
        if value.get("type") != "search":
            raise ValueError("search response has no search result envelope")
        return ()
    if not isinstance(web, dict) or not isinstance(web.get("results"), list):
        raise ValueError("invalid search results")
    results = []
    encoded_bytes = 0
    for item in web["results"][:maximum]:
        if not isinstance(item, dict):
            raise ValueError("invalid search hit")
        url, title, snippet = item.get("url"), item.get("title", ""), item.get("description", "")
        if snippet is None:
            snippet = ""
        if not all(isinstance(field, str) for field in (url, title, snippet)):
            raise ValueError("invalid search hit fields")
        if not url or len(url) > 4096 or any(character in url for character in ("\0", "\r", "\n")):
            continue
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            continue
        hit = SearchHit(title.replace("\0", "")[:1000], url, snippet.replace("\0", "")[:2048])
        size = len(
            json.dumps(
                {"title": hit.title, "url": hit.url, "snippet": hit.snippet}, ensure_ascii=False
            ).encode()
        )
        if encoded_bytes + size > 17_000:
            break
        results.append(hit)
        encoded_bytes += size
    return tuple(results)


class BraveSearchProvider:
    def __init__(
        self, api_key: str | None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        key = api_key.strip() if api_key is not None else ""
        if key and (
            len(key) > 4096 or not key.isascii() or any(character.isspace() for character in key)
        ):
            raise ValueError("invalid Brave credential format")
        self._key = key
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @property
    def key_hash(self) -> str | None:
        return hashlib.sha256(self._key.encode()).hexdigest() if self._key else None

    async def search(
        self, query: str, max_results: int, context: ToolExecutionContext
    ) -> SearchResult:
        await context.assert_active()
        if not self.configured:
            return SearchResult(SearchStatus.UNAVAILABLE, error_code="brave_key_missing")
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= MAX_SEARCH_QUERY_CHARS
            or len(query.split()) > 75
            or any(character in query for character in ("\0", "\r", "\n"))
            or type(max_results) is not int
            or not 1 <= max_results <= MAX_SEARCH_RESULTS
        ):
            return SearchResult(SearchStatus.PROVIDER_ERROR, error_code="invalid_search_arguments")
        received = bytearray()
        try:
            async with (
                asyncio.timeout(min(30.0, context.remaining_seconds)),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(20.0, connect=5.0),
                    transport=self._transport,
                ) as client,
                client.stream(
                    "POST",
                    BRAVE_SEARCH_ENDPOINT,
                    json={
                        "q": query.strip(),
                        "count": max_results,
                        "text_decorations": False,
                        "result_filter": ["web"],
                    },
                    headers={
                        "X-Subscription-Token": self._key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    return SearchResult(SearchStatus.INVALID_KEY, error_code="brave_access_denied")
                if response.status_code == 429:
                    return SearchResult(SearchStatus.RATE_LIMITED, error_code="brave_rate_limited")
                if response.status_code != 200:
                    return SearchResult(SearchStatus.PROVIDER_ERROR, error_code="brave_http_error")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return SearchResult(
                        SearchStatus.PROVIDER_ERROR, error_code="brave_unsupported_encoding"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None and (
                    not content_length.isdecimal()
                    or int(content_length) > MAX_SEARCH_RESPONSE_BYTES
                ):
                    return SearchResult(
                        SearchStatus.OVERSIZED, error_code="brave_response_oversized"
                    )
                async for chunk in response.aiter_raw():
                    if len(received) + len(chunk) > MAX_SEARCH_RESPONSE_BYTES:
                        return SearchResult(
                            SearchStatus.OVERSIZED,
                            error_code="brave_response_oversized",
                            response_bytes=MAX_SEARCH_RESPONSE_BYTES,
                        )
                    received.extend(chunk)
                hits = _hits(bytes(received), max_results)
            await context.assert_active()
            return SearchResult(SearchStatus.SUCCESS, hits, response_bytes=len(received))
        except TimeoutError, httpx.TimeoutException:
            return SearchResult(
                SearchStatus.TIMEOUT, error_code="brave_timeout", response_bytes=len(received)
            )
        except httpx.HTTPError, ValueError, UnicodeError, RecursionError:
            return SearchResult(
                SearchStatus.PROVIDER_ERROR,
                error_code="brave_invalid_response",
                response_bytes=len(received),
            )
