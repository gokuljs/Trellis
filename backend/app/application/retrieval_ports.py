"""Isolated retrieval is separate from application-authenticated HTTP clients."""

from typing import Protocol

from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import RetrievalRequest, RetrievalResult


class PublicRetriever(Protocol):
    async def retrieve(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> RetrievalResult: ...
