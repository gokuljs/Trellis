"""Isolated retrieval is separate from application-authenticated HTTP clients."""

from typing import Protocol

from app.application.tool_context import ToolExecutionContext
from app.domain.retrieval import (
    ExtractedDocumentResult,
    RenderRequest,
    RetrievalRequest,
    RetrievalResult,
)


class PublicRetriever(Protocol):
    async def retrieve(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> RetrievalResult: ...


class DocumentRetriever(Protocol):
    async def is_ready(self) -> bool: ...

    async def read_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult: ...


class BrowserRenderer(Protocol):
    async def is_render_ready(self) -> bool: ...

    async def render_document(
        self, request: RenderRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult: ...


class CrawlRetriever(Protocol):
    async def is_crawl_ready(self) -> bool: ...

    async def crawl_document(
        self, request: RetrievalRequest, context: ToolExecutionContext
    ) -> ExtractedDocumentResult: ...
