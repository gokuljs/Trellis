"""Fixed renderer operation inside the inspected networkless Chromium worker."""

import asyncio
import importlib
import json
import re
import ssl
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import h11

from app.domain.retrieval import (
    MAX_EXTRACTED_RESULT_BYTES,
    MAX_WORKER_REQUEST_BYTES,
    ExtractedDocumentResult,
    RenderRequest,
    RetrievalStatus,
)
from app.domain.web import SourceStatus, WebUsage
from app.infrastructure.browser_http import BrowserHTTPClient
from app.infrastructure.web_extraction import (
    _document_url,
    _metadata,
    _publication_date,
    bounded_source_text,
)
from app.infrastructure.web_network import DestinationBlocked
from app.infrastructure.web_reader import _RetrievalFailure

_EXTRACT = """() => {
  const root = document.querySelector('main') || document.querySelector('article') || document.body;
  const text = root ? root.innerText : '';
  const encoded = new TextEncoder().encode(text);
  const meta = name => document.querySelector(
    `meta[property="${name}"],meta[name="${name}"]`)?.content || null;
  return {
    text: new TextDecoder().decode(encoded.slice(0, 262144)), limited: encoded.length > 262144,
    title: document.title.slice(0,1000),
    publisher: (meta('og:site_name') || meta('publisher') || '').slice(0,500),
    published: (meta('article:published_time') || meta('datepublished') ||
      document.querySelector('time[datetime]')?.dateTime || '').slice(0,100),
    canonical: (document.querySelector('link[rel="canonical"]')?.href || '').slice(0,4096),
    links: Array.from(document.querySelectorAll('a[href]')).slice(0,32)
      .map(a => a.href.slice(0,4096))
  };
}"""
_ARGS = [
    "--disable-quic",
    "--disable-background-networking",
    "--disable-breakpad",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]


def _playwright() -> Any:
    # The SDK and browser exist only in the worker image.
    return importlib.import_module("playwright.async_api").async_playwright()


async def sandbox_probe() -> dict[str, object]:
    async with _playwright() as runtime:
        browser = await runtime.chromium.launch(
            channel="chromium", headless=True, chromium_sandbox=True, args=_ARGS
        )
        try:
            page = await browser.new_page()
            await page.goto("chrome://sandbox")
            text = await page.locator("body").inner_text(timeout=5000)
            if not all(
                re.search(re.escape(marker) + r"\s+Yes\b", text)
                for marker in ("Seccomp-BPF sandbox", "PID namespaces", "Network namespaces")
            ):
                raise ValueError("Chromium sandbox diagnostics unavailable")
            if "adequately sandboxed" not in text or "not adequately sandboxed" in text:
                raise ValueError("Chromium sandbox is inactive")
            return {"protocol": "public-render-v1", "sandbox": True}
        finally:
            await browser.close()


async def _load_page(page: Any, url: str, errors: list[str]) -> None:
    timeout_error = importlib.import_module("playwright.async_api").TimeoutError
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except timeout_error as error:
        raise _RetrievalFailure(RetrievalStatus.TIMEOUT, "browser_navigation_timeout") from error
    try:
        # Background polling must not prevent extraction of an already loaded DOM.
        await page.wait_for_load_state("networkidle", timeout=3000)
    except timeout_error:
        errors.append("browser_settle_timeout")


async def render(request: RenderRequest, client: BrowserHTTPClient) -> ExtractedDocumentResult:
    client.configure(request)
    errors: list[str] = []
    browser = None
    try:
        async with asyncio.timeout(request.timeout_seconds):
            document = await client.document()
            if document.media_type != "text/html":
                raise _RetrievalFailure(RetrievalStatus.UNSUPPORTED, "render_requires_html")
            async with _playwright() as runtime:
                browser = await runtime.chromium.launch(
                    channel="chromium",
                    headless=True,
                    chromium_sandbox=True,
                    args=_ARGS,
                    proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                )
                context = await browser.new_context(
                    service_workers="block",
                    accept_downloads=False,
                    java_script_enabled=True,
                    ignore_https_errors=False,
                )
                context.set_default_timeout(5000)
                page: Any = None
                cached = True

                async def route_resource(route: Any, incoming: Any) -> None:
                    nonlocal cached
                    try:
                        if incoming.method not in {"GET", "HEAD"}:
                            raise _RetrievalFailure(
                                RetrievalStatus.BLOCKED, "browser_method_blocked"
                            )
                        if incoming.resource_type in {"image", "media", "font"}:
                            await route.abort("blockedbyclient")
                            return
                        if incoming.is_navigation_request():
                            if (
                                incoming.frame != page.main_frame
                                or not cached
                                or incoming.url != document.url
                            ):
                                raise _RetrievalFailure(
                                    RetrievalStatus.BLOCKED, "browser_navigation_blocked"
                                )
                            response = document
                            cached = False
                        else:
                            response = await client.fetch(incoming.url, incoming.method)
                        if 300 <= response.status < 400:
                            raise _RetrievalFailure(
                                RetrievalStatus.BLOCKED, "browser_resource_redirect_blocked"
                            )
                        await route.fulfill(
                            status=response.status,
                            headers=dict(response.headers),
                            body=response.body,
                        )
                    except _RetrievalFailure as error:
                        errors.append(error.code)
                        await route.abort("blockedbyclient")
                    except DestinationBlocked as error:
                        errors.append(str(error))
                        await route.abort("blockedbyclient")
                    except OSError, ValueError, h11.RemoteProtocolError:
                        errors.append("browser_resource_unavailable")
                        await route.abort("failed")

                await context.route("**/*", route_resource)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                page = await context.new_page()
                context.on(
                    "page",
                    lambda popup: asyncio.create_task(popup.close()) if popup != page else None,
                )
                await _load_page(page, document.url, errors)
                value = await page.evaluate(_EXTRACT)
                if not isinstance(value, dict):
                    raise ValueError("invalid DOM extraction")
                text, limited = bounded_source_text(value["text"])
                if not text:
                    return ExtractedDocumentResult(
                        SourceStatus.EXTRACTION_FAILED,
                        request.url,
                        document.url,
                        "text/html",
                        error_code="document_has_no_extractable_text",
                        usage=WebUsage(
                            page_attempts=1,
                            network_requests=client.requests,
                            response_bytes=client.response_bytes,
                        ),
                        extractor_version="trellis-render-v1",
                    )
                limited = limited or bool(value["limited"])
                links = tuple(
                    dict.fromkeys(
                        link
                        for candidate in value["links"]
                        if (link := _document_url(candidate, document.url)) is not None
                    )
                )
                return ExtractedDocumentResult(
                    SourceStatus.PARTIAL if limited or errors else SourceStatus.SUCCESS,
                    request.url,
                    document.url,
                    "text/html",
                    text,
                    WebUsage(
                        page_attempts=1,
                        network_requests=client.requests,
                        response_bytes=client.response_bytes,
                        extracted_bytes=len(text.encode()),
                    ),
                    "extracted_text_limit" if limited else errors[0] if errors else None,
                    _metadata(value["title"], 1000),
                    _metadata(value["publisher"], 500),
                    _publication_date(value["published"]),
                    _document_url(value["canonical"], document.url),
                    links,
                    "trellis-render-v1",
                )
    except DestinationBlocked as error:
        status, code = SourceStatus.BLOCKED, str(error)
    except _RetrievalFailure as error:
        status = (
            SourceStatus.BLOCKED
            if error.status is RetrievalStatus.ACCESS_DENIED
            else SourceStatus(error.status.value)
        )
        code = error.code
    except TimeoutError:
        status, code = SourceStatus.TIMEOUT, "browser_timeout"
    except ssl.SSLError:
        status, code = SourceStatus.UNAVAILABLE, "tls_verification_failed"
    except Exception:
        status, code = SourceStatus.UNAVAILABLE, "browser_unavailable"
    finally:
        if browser is not None:
            await browser.close()
    return ExtractedDocumentResult(
        status,
        request.url,
        error_code=code,
        usage=WebUsage(
            page_attempts=1, network_requests=client.requests, response_bytes=client.response_bytes
        ),
        extractor_version="trellis-render-v1",
    )


async def _run(payload: object) -> ExtractedDocumentResult | dict[str, object]:
    if payload == {"operation": "sandbox_probe"}:
        return await sandbox_probe()
    if not isinstance(payload, dict) or payload.pop("operation", None) != "render":
        raise ValueError("invalid browser operation")
    if set(payload) != {
        "url",
        "max_requests",
        "max_response_bytes",
        "timeout_seconds",
        "scope_origin",
        "scope_path_prefix",
        "gateway_socket",
        "lease_token",
    }:
        raise ValueError("invalid browser request fields")
    socket, token = payload.pop("gateway_socket"), payload.pop("lease_token")
    if socket != "/run/trellis-egress/gateway.sock" or not isinstance(token, str):
        raise ValueError("invalid browser gateway")
    return await render(RenderRequest(**payload), BrowserHTTPClient(Path(socket), token))


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
    if len(raw) > MAX_WORKER_REQUEST_BYTES:
        raise ValueError("browser request exceeds limit")
    result = asyncio.run(_run(json.loads(raw)))
    output = json.dumps(
        asdict(result) if isinstance(result, ExtractedDocumentResult) else result,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(output.encode()) > MAX_EXTRACTED_RESULT_BYTES:
        raise ValueError("browser result exceeds limit")
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
