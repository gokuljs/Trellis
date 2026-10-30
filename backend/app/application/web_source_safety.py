"""The redaction boundary shared by individual reads and crawl evidence."""

import json
from dataclasses import replace
from urllib.parse import unquote_plus

from app.application.secret_redaction import SecretRedactor
from app.domain.crawl import normalize_crawl_url
from app.domain.retrieval import MAX_DOCUMENT_LINKS
from app.domain.web import MAX_SOURCE_BYTES, SourceDraft, SourceStatus, canonical_source_text

LINKS_VERSION = "+links-v1"
LINKS_HEADER = "Links discovered in this page (destinations have not necessarily been read):"
LINKS_FOOTER = "Discovered link limits: "
MAX_LINK_SECTION_BYTES = 8 * 1024
MAX_LINK_SECTION_LINES = MAX_DOCUMENT_LINKS * 2 + 2


def sensitive_url(value: str, redactor: SecretRedactor) -> bool:
    decoded = value
    for _ in range(5):
        if redactor.redact_text(decoded) != decoded:
            return True
        try:
            expanded = unquote_plus(decoded, errors="strict")
        except UnicodeError:
            return True
        if expanded == decoded:
            return False
        decoded = expanded
    return True


def accepted_source(
    draft: SourceDraft,
    redactor: SecretRedactor,
    *,
    discovered_links: tuple[str, ...] | None = None,
) -> SourceDraft:
    text = canonical_source_text(redactor.redact_source_text(draft.text))
    text = text.encode()[:MAX_SOURCE_BYTES].decode(errors="ignore")
    accepted = replace(
        draft,
        text=text,
        requested_url=redactor.redact_source_text(draft.requested_url)[:4096],
        **{
            name: None
            if (value := getattr(draft, name)) is None
            else redactor.redact_source_text(value)[:limit]
            for name, limit in (
                ("final_url", 4096),
                ("canonical_url", 4096),
                ("title", 1000),
                ("publisher", 500),
                ("published_at", 100),
                ("extractor_version", 100),
                ("error_code", 100),
            )
        },
    )
    if accepted != draft and draft.status.citable:
        accepted = replace(
            accepted, status=SourceStatus.PARTIAL, error_code="sensitive_content_redacted"
        )
    if discovered_links is None or not accepted.status.citable:
        return accepted
    links: list[str] = []
    filtered = 0
    for value in discovered_links[:MAX_DOCUMENT_LINKS]:
        try:
            link = normalize_crawl_url(value)
        except ValueError, UnicodeError:
            filtered += 1
            continue
        if sensitive_url(value, redactor) or sensitive_url(link, redactor):
            filtered += 1
        elif link not in links:
            links.append(link)
    omitted = 0

    def section(content_limited: bool) -> str:
        metadata = {
            "lengths": [len(link) for link in links],
            "filtered": filtered,
            "omitted": omitted,
            "at_limit": len(discovered_links) >= MAX_DOCUMENT_LINKS,
            "content_limited": content_limited,
        }
        return canonical_source_text(
            "\n".join(
                (LINKS_HEADER, *links, LINKS_FOOTER + json.dumps(metadata, separators=(",", ":")))
            )
        )

    while len(section(False).encode()) > MAX_LINK_SECTION_BYTES:
        links.pop()
        omitted += 1
    suffix = section(False)
    available = MAX_SOURCE_BYTES - len(suffix.encode()) - 2
    limited = len(accepted.text.encode()) > available
    content = accepted.text
    if limited:
        content = content.encode()[:available].decode(errors="ignore").rsplit("\n", 1)[0]
        suffix = section(True)
    return replace(
        accepted,
        text=canonical_source_text(content.rstrip("\n") + "\n\n" + suffix),
        extractor_version=accepted.extractor_version[: 100 - len(LINKS_VERSION)] + LINKS_VERSION,
        status=SourceStatus.PARTIAL if limited else accepted.status,
        error_code=(accepted.error_code or "extracted_text_limit")
        if limited
        else accepted.error_code,
    )
