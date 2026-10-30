"""Small evidence inventories backed by immutable source lines."""

import json
from math import ceil
from typing import cast

from app.application.secret_redaction import SecretRedactor
from app.application.tools import MAX_RESULT_BYTES
from app.application.web_ports import WebRepository
from app.application.web_source_safety import (
    LINKS_FOOTER,
    LINKS_HEADER,
    LINKS_VERSION,
    MAX_LINK_SECTION_LINES,
    sensitive_url,
)
from app.domain.crawl import normalize_crawl_url
from app.domain.retrieval import MAX_DOCUMENT_LINKS
from app.domain.web import MAX_SOURCE_LINE_CHARS, SourceLines


async def source_summary(
    repository: WebRepository, result: SourceLines, redactor: SecretRedactor
) -> dict[str, object]:
    source = result.source
    lines: list[dict[str, object]] = []
    for offset, line in enumerate(result.lines):
        safe = redactor.redact_text(line)
        text = safe.encode()[:600].decode(errors="ignore")
        lines.append(
            {"line": result.start_line + offset, "text": text, "text_truncated": text != safe}
        )
    value: dict[str, object] = {
        "source_id": source.id,
        "status": source.status.value,
        "error_code": source.error_code,
        "content_hash": source.content_hash,
        "total_lines": source.line_count,
        "untrusted_excerpt": lines,
        "display_redacted": source.error_code == "sensitive_content_redacted"
        or any(redactor.redact_text(line) != line for line in result.lines),
        "title": (source.title or "")[:300] or None,
        "final_url": source.final_url
        if source.final_url is None or len(source.final_url.encode()) <= 1024
        else None,
        "final_url_available_in_source": source.final_url is not None,
        "final_url_omitted": source.final_url is not None and len(source.final_url.encode()) > 1024,
        "publisher": (source.publisher or "")[:100] or None,
        "published_at": source.published_at,
        "fetched_at": source.fetched_at,
        "discovered_links_available": False,
    }
    if source.status.citable and (source.extractor_version or "").endswith(LINKS_VERSION):
        ending = await repository.read_web_source(
            source.session_id, source.id, source.line_count, 1
        )
        metadata = _link_metadata(ending.lines[-1]) if ending and ending.lines else None
        if metadata is not None:
            count = 2 + sum(
                ceil(length / MAX_SOURCE_LINE_CHARS)
                for length in cast(list[int], metadata["lengths"])
            )
            if count <= min(source.line_count, MAX_LINK_SECTION_LINES):
                tail = await repository.read_web_source(
                    source.session_id, source.id, source.line_count - count + 1, count
                )
                if tail is not None:
                    value.update(_discovered_links(tail, redactor))
    value["display_redacted"] = bool(value["display_redacted"]) or (
        redactor.redact_record(value) != value
    )
    return value


def _link_metadata(footer: str) -> dict[str, object] | None:
    try:
        if not footer.startswith(LINKS_FOOTER):
            return None
        metadata = json.loads(footer[len(LINKS_FOOTER) :])
        lengths = metadata["lengths"]
        if (
            not isinstance(lengths, list)
            or len(lengths) > MAX_DOCUMENT_LINKS
            or any(type(length) is not int or not 1 <= length <= 4096 for length in lengths)
            or any(type(metadata[key]) is not bool for key in ("at_limit", "content_limited"))
            or any(
                type(metadata[key]) is not int or not 0 <= metadata[key] <= MAX_DOCUMENT_LINKS
                for key in ("filtered", "omitted")
            )
        ):
            return None
        return cast(dict[str, object], metadata)
    except ValueError, KeyError, TypeError:
        return None


def _discovered_links(tail: SourceLines, redactor: SecretRedactor) -> dict[str, object]:
    try:
        header = len(tail.lines) - 1 - tail.lines[::-1].index(LINKS_HEADER)
        metadata = _link_metadata(tail.lines[-1])
        if metadata is None:
            return {}
        cursor = header + 1
        links: list[dict[str, object]] = []
        for length in cast(list[int], metadata["lengths"]):
            count = ceil(length / MAX_SOURCE_LINE_CHARS)
            url = "".join(tail.lines[cursor : cursor + count])
            if (
                len(url) != length
                or normalize_crawl_url(url) != url
                or sensitive_url(url, redactor)
            ):
                return {}
            links.append(
                {
                    "url": url,
                    "start_line": tail.start_line + cursor,
                    "end_line": tail.start_line + cursor + count - 1,
                }
            )
            cursor += count
        if cursor != len(tail.lines) - 1:
            return {}
        return {
            "discovered_links_available": True,
            "untrusted_discovered_links": links,
            "discovered_links_count": len(links),
            "discovered_links_start_line": tail.start_line + header + 1 if links else None,
            "discovered_links_end_line": tail.start_line + cursor - 1 if links else None,
            "discovered_link_limit": MAX_DOCUMENT_LINKS,
            "discovered_links_may_be_incomplete": bool(
                metadata["at_limit"] or metadata["filtered"] or metadata["omitted"]
            ),
            "discovered_links_omitted_from_source": metadata["omitted"],
            "discovered_links_filtered": metadata["filtered"],
            "content_limited": metadata["content_limited"],
        }
    except ValueError, KeyError, TypeError, IndexError:
        return {}


def bound_source_summaries(
    value: dict[str, object], pages: list[dict[str, object]], redactor: SecretRedactor
) -> dict[str, object]:
    def encoded_size() -> int:
        return len(
            json.dumps(
                redactor.redact_record(value), ensure_ascii=False, separators=(",", ":")
            ).encode()
        )

    while encoded_size() >= MAX_RESULT_BYTES:
        for field in ("untrusted_excerpt", "untrusted_discovered_links"):
            candidates = [page for page in pages if page.get(field)]
            if candidates:
                page = max(candidates, key=lambda item: len(json.dumps(item, ensure_ascii=False)))
                items = cast(list[object], page[field])
                removed = max(1, len(items) // 2)
                del items[-removed:]
                if field == "untrusted_excerpt":
                    page["excerpt_omitted"] = True
                else:
                    page["discovered_links_omitted_from_result"] = (
                        cast(int, page.get("discovered_links_omitted_from_result", 0)) + removed
                    )
                break
        else:
            for field in ("final_url", "title", "publisher"):
                if page := next((page for page in pages if page.get(field) is not None), None):
                    page[field] = None
                    page[f"{field}_omitted"] = True
                    break
            else:
                raise ValueError("source inventory exceeds its bounded metadata contract")
    return value
