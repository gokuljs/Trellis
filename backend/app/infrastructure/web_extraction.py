"""Worker-side extraction; PDF parsing runs in a separately bounded process."""

import asyncio
import json
import re
import sys
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.domain.retrieval import (
    MAX_DOCUMENT_LINKS,
    MAX_EXTRACTED_RESULT_BYTES,
    ExtractedDocumentResult,
    RetrievalResult,
    RetrievalStatus,
)
from app.domain.web import MAX_SOURCE_BYTES, SourceStatus, canonical_source_text
from app.infrastructure.web_network import DestinationBlocked, parse_public_url

PDF_PROCESS_SECONDS = 10.0
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLOCK_TAGS = frozenset(
    {
        "address",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "main",
        "article",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_OMIT_TAGS = frozenset(
    {
        "script",
        "style",
        "template",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        "svg",
        "canvas",
        "form",
        "iframe",
        "object",
        "embed",
        "head",
        "title",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def bounded_source_text(text: str) -> tuple[str, bool]:
    canonical = canonical_source_text(_CONTROL_CHARACTERS.sub("", text))
    encoded = canonical.encode("utf-8", errors="replace")
    bounded = encoded[:MAX_SOURCE_BYTES].decode("utf-8", errors="ignore")
    return bounded, len(encoded) > MAX_SOURCE_BYTES


def _metadata(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    value = " ".join(_CONTROL_CHARACTERS.sub("", value).split())[:maximum]
    return value or None


def _publication_date(value: str | None) -> str | None:
    value = _metadata(value, 100)
    if value is None:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _document_url(value: str | None, base: str) -> str | None:
    if not value or len(value) > 4096:
        return None
    try:
        url = parse_public_url(urljoin(base, value)).url
        return url if len(url.encode("utf-8")) <= 4096 else None
    except ValueError, DestinationBlocked:
        return None


class _HTMLDocument(HTMLParser):
    def __init__(self, final_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._final_url = final_url
        self._stack: list[tuple[str, bool]] = []
        self._all: list[str] = []
        self._main: list[str] = []
        self._article: list[str] = []
        self._title: list[str] = []
        self.publisher: str | None = None
        self.published_at: str | None = None
        self.canonical_url: str | None = None
        self.links: list[str] = []

    def _append(self, text: str) -> None:
        if self._stack and self._stack[-1][1]:
            return
        self._all.append(text)
        if any(tag == "main" for tag, _blocked in self._stack):
            self._main.append(text)
        if any(tag == "article" for tag, _blocked in self._stack):
            self._article.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if len(self._stack) >= 256:
            raise ValueError("HTML nesting limit")
        parent_blocked = bool(self._stack and self._stack[-1][1])
        if tag == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").casefold()
            content = attributes.get("content")
            if name in {"og:site_name", "publisher"} and self.publisher is None:
                self.publisher = _metadata(content, 500)
            if name in {"article:published_time", "datepublished", "date", "pubdate"}:
                self.published_at = self.published_at or _publication_date(content)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            self.canonical_url = self.canonical_url or _document_url(
                attributes.get("href"), self._final_url
            )
        if tag == "time" and not parent_blocked:
            self.published_at = self.published_at or _publication_date(attributes.get("datetime"))
        if tag == "a" and len(self.links) < MAX_DOCUMENT_LINKS:
            link = _document_url(attributes.get("href"), self._final_url)
            if link is not None and link not in self.links:
                self.links.append(link)
        if tag in _BLOCK_TAGS:
            self._append("\n")
        blocked = (
            parent_blocked
            or tag in _OMIT_TAGS
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").casefold() == "true"
        )
        if tag not in _VOID_TAGS:
            self._stack.append((tag, blocked))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                if tag in _BLOCK_TAGS:
                    self._append("\n")
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][0] == "title":
            self._title.append(data)
        self._append(data)

    @property
    def title(self) -> str | None:
        return _metadata("".join(self._title), 1000)

    @staticmethod
    def _clean(parts: list[str]) -> str:
        return "\n".join(
            clean for line in "".join(parts).splitlines() if (clean := " ".join(line.split()))
        )

    @property
    def text(self) -> str:
        return self._clean(self._main) or self._clean(self._article) or self._clean(self._all)


async def _read_parser_output(reader: asyncio.StreamReader) -> bytes:
    payload = bytearray()
    while chunk := await reader.read(min(65536, MAX_EXTRACTED_RESULT_BYTES + 1 - len(payload))):
        payload.extend(chunk)
        if len(payload) > MAX_EXTRACTED_RESULT_BYTES:
            raise ValueError("PDF parser result exceeds limit")
    return bytes(payload)


async def _extract_pdf(
    body: bytes, timeout_seconds: float
) -> tuple[SourceStatus, str, str | None, str | None]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.infrastructure.pdf_extractor",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        async with asyncio.timeout(min(PDF_PROCESS_SECONDS, timeout_seconds)):
            output = asyncio.create_task(_read_parser_output(process.stdout))
            try:
                process.stdin.write(body)
                await process.stdin.drain()
                process.stdin.close()
                code = await process.wait()
                payload = await output
            finally:
                if not output.done():
                    output.cancel()
                with suppress(asyncio.CancelledError):
                    await output
            if code:
                return SourceStatus.EXTRACTION_FAILED, "", None, "pdf_resource_limit"
        result = json.loads(payload)
        if not isinstance(result, dict) or set(result) != {"status", "text", "title", "error_code"}:
            raise ValueError("invalid PDF parser result")
        if (
            not isinstance(result["text"], str)
            or (result["title"] is not None and not isinstance(result["title"], str))
            or (result["error_code"] is not None and not isinstance(result["error_code"], str))
        ):
            raise ValueError("invalid PDF parser fields")
        return SourceStatus(result["status"]), result["text"], result["title"], result["error_code"]
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        reaping = asyncio.create_task(process.wait())
        cancelled = False
        while not reaping.done():
            try:
                await asyncio.shield(reaping)
            except asyncio.CancelledError:
                cancelled = True
        reaping.result()
        if cancelled:
            raise asyncio.CancelledError


async def extract_document(
    raw: RetrievalResult, *, timeout_seconds: float
) -> ExtractedDocumentResult:
    status = (
        SourceStatus.BLOCKED
        if raw.status is RetrievalStatus.ACCESS_DENIED
        else SourceStatus(raw.status.value)
    )
    base = ExtractedDocumentResult(
        status if status is not SourceStatus.SUCCESS else SourceStatus.EXTRACTION_FAILED,
        raw.requested_url,
        raw.final_url,
        raw.media_type,
        usage=raw.usage,
        error_code=raw.error_code,
    )
    if raw.status is not RetrievalStatus.SUCCESS:
        return base
    try:
        if raw.media_type == "application/pdf":
            status, parsed_text, title, error_code = await _extract_pdf(raw.body, timeout_seconds)
            text, limited = bounded_source_text(parsed_text)
            return replace(
                base,
                status=SourceStatus.PARTIAL if limited and status.citable else status,
                text=text,
                title=_metadata(title, 1000),
                error_code="extracted_text_limit" if limited else error_code,
                usage=replace(raw.usage, extracted_bytes=len(text.encode("utf-8"))),
            )
        html = None
        decoded = raw.body.decode("utf-8-sig", errors="replace")
        if raw.media_type == "text/html":
            html = _HTMLDocument(raw.final_url or raw.requested_url)
            html.feed(decoded)
            html.close()
            decoded = html.text
        text, limited = bounded_source_text(decoded.strip())
        if not text:
            return replace(base, error_code="document_has_no_extractable_text")
        return replace(
            base,
            status=SourceStatus.PARTIAL if limited else SourceStatus.SUCCESS,
            text=text,
            title=html.title if html is not None else None,
            publisher=html.publisher if html is not None else None,
            published_at=html.published_at if html is not None else None,
            canonical_url=html.canonical_url if html is not None else None,
            links=tuple(html.links) if html is not None else (),
            error_code="extracted_text_limit" if limited else None,
            usage=replace(raw.usage, extracted_bytes=len(text.encode("utf-8"))),
        )
    except TimeoutError:
        return replace(base, status=SourceStatus.TIMEOUT, error_code="pdf_parser_timeout")
    except OSError, ValueError, KeyError, TypeError:
        return replace(base, error_code="document_extraction_failed")
