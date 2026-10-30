"""Conversation-owned access to recorded web evidence."""

from dataclasses import asdict

from app.application.errors import ApplicationError
from app.application.ports import SessionRepository
from app.application.tool_context import ToolExecutionContext
from app.application.web_ports import WebRepository
from app.domain.web import (
    MAX_SOURCE_LINES,
    CitationBinding,
    ClearSourcesResult,
    SourceLines,
    SourcePage,
    WebDispatch,
    WebScope,
)

WEB_ERROR_MESSAGES = {
    "web_search_budget_exhausted": "The operator search allowance for this month was reached.",
    "web_budget_exhausted": "The run reached its web collection allowance.",
    "web_sources_busy": "Finish or cancel the active run before clearing sources.",
    "web_source_conflict": "The recorded source conflicts with this operation.",
    "web_scope_invalid": "The web operation is no longer active in this conversation.",
    "web_storage_limit": "The saved web evidence limit was reached. Clear sources to make space.",
    "web_frontier_limit": "The crawl reached its queued-page limit.",
    "citation_invalid": "A citation does not refer to available supporting source lines.",
    "source_not_found": "Source not found.",
    "web_invalid_range": "The requested source line range is invalid.",
}


class WebEvidenceService:
    def __init__(
        self, repository: WebRepository, sessions: SessionRepository, *, owner_id: str
    ) -> None:
        self.repository = repository
        self._sessions = sessions
        self._owner_id = owner_id

    async def _require_session(self, session_id: str) -> None:
        session = await self._sessions.get_session(session_id)
        if session is None or session.user_id != self._owner_id:
            raise ApplicationError("session_not_found", "Session not found.")

    async def open_scope(self, context: ToolExecutionContext) -> WebScope:
        await context.assert_active()
        if context.user_id != self._owner_id:
            raise ApplicationError("web_scope_invalid", WEB_ERROR_MESSAGES["web_scope_invalid"])
        dispatch = WebDispatch(
            context.user_id,
            context.session_id,
            context.run_id,
            context.tool_call_id,
            context.dispatch_id,
            context.dispatch_attempt,
            context.generation,
        )
        state = await self.repository.open_web_run(dispatch)
        return WebScope(**asdict(dispatch), evidence_epoch=state.evidence_epoch)

    async def list_sources(self, session_id: str, offset: int, limit: int) -> SourcePage:
        await self._require_session(session_id)
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 50:
            raise ApplicationError("web_invalid_range", WEB_ERROR_MESSAGES["web_invalid_range"])
        return await self.repository.list_web_sources(session_id, offset, limit)

    async def read_source(
        self, session_id: str, source_id: str, start_line: int, max_lines: int
    ) -> SourceLines:
        await self._require_session(session_id)
        if not 1 <= start_line <= MAX_SOURCE_LINES or not 1 <= max_lines <= 200:
            raise ApplicationError("web_invalid_range", WEB_ERROR_MESSAGES["web_invalid_range"])
        result = await self.repository.read_web_source(session_id, source_id, start_line, max_lines)
        if result is None:
            raise ApplicationError("source_not_found", WEB_ERROR_MESSAGES["source_not_found"])
        return result

    async def clear_sources(self, session_id: str) -> ClearSourcesResult:
        await self._require_session(session_id)
        return await self.repository.clear_web_sources(session_id)

    async def read_citations(
        self, session_id: str, model_call_id: str
    ) -> tuple[CitationBinding, ...]:
        await self._require_session(session_id)
        return await self.repository.read_web_citations(session_id, model_call_id)
