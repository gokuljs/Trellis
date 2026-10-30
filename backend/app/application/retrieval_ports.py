"""Isolated retrieval is separate from application-authenticated HTTP clients."""

from typing import Protocol

from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import ExtractedDocumentResult, RetrievalRequest, RetrievalResult


class PublicRetriever(Protocol):
    async def retrieve(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> RetrievalResult: ...


class DocumentRetriever(Protocol):
    async def is_ready(self) -> bool: ...

    async def read_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult: ...
