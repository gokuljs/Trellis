from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import Settings
from app.domain.web import SourceDraft, SourceStatus, WebScope, WebUsage
from app.infrastructure.supabase_repository import SupabaseRepository, VerifiedTokenSource

OWNER = "30000000-0000-4000-8000-000000000001"
SESSION = "30000000-0000-4000-8000-000000000002"
RUN = "30000000-0000-4000-8000-000000000003"
TOOL = "30000000-0000-4000-8000-000000000004"
DISPATCH = "30000000-0000-4000-8000-000000000005"
SOURCE = "30000000-0000-4000-8000-000000000006"
RESERVATION = "30000000-0000-4000-8000-000000000007"
OTHER = "30000000-0000-4000-8000-000000000008"


def source_draft(**changes):
    return replace(
        SourceDraft(
            SOURCE,
            "https://example.com/start",
            "https://example.com/article",
            None,
            "Example article",
            None,
            None,
            "2026-10-30T00:00:00Z",
            "html-v1",
            "First supporting line.\nSecond supporting line.",
            SourceStatus.SUCCESS,
        ),
        **changes,
    )


def source_row(**changes):
    draft = source_draft()
    return {
        "id": draft.id,
        "user_id": OWNER,
        "session_id": SESSION,
        "run_id": RUN,
        "tool_call_id": TOOL,
        "evidence_epoch": 0,
        "requested_url": draft.requested_url,
        "final_url": draft.final_url,
        "canonical_url": None,
        "title": draft.title,
        "publisher": None,
        "published_at": None,
        "fetched_at": draft.fetched_at,
        "extractor_version": draft.extractor_version,
        "content_hash": draft.content_hash,
        "line_count": 2,
        "byte_count": len(draft.text.encode()),
        "status": "success",
        "error_code": None,
        "created_at": "2026-10-30T00:00:00Z",
        **changes,
    }


def scope():
    return WebScope(OWNER, SESSION, RUN, TOOL, DISPATCH, 1, 2, 0)


def reservation_row(usage=None, **changes):
    return {
        **scope().to_dict(),
        "id": RESERVATION,
        "operation_key": "page:1",
        "reserved": (usage or WebUsage(page_attempts=1, extracted_bytes=100)).to_dict(),
        "actual": None,
        "status": "reserved",
        **changes,
    }


def repository(client):
    tokens = VerifiedTokenSource(UUID(OWNER))
    tokens.record_verified(
        "verified-user-token", UUID(OWNER), datetime.now(UTC) + timedelta(hours=1)
    )
    result = SupabaseRepository(
        Settings(
            _env_file=None,
            supabase_url="https://test.supabase.co",
            supabase_publishable_key="sb_publishable_fixture",
        ),
        UUID(OWNER),
        tokens,
        client,
    )
    result._capabilities[RUN] = "private-run-capability"
    result._lease_expires[RUN] = datetime.now(UTC) + timedelta(minutes=5)
    return result
