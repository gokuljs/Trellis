"""The redaction boundary shared by individual reads and crawl evidence."""

from dataclasses import replace
from urllib.parse import unquote_plus

from app.application.secret_redaction import SecretRedactor
from app.domain.web import MAX_SOURCE_BYTES, SourceDraft, SourceStatus, canonical_source_text


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


def accepted_source(draft: SourceDraft, redactor: SecretRedactor) -> SourceDraft:
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
    return accepted
