"""Search discovers candidate URLs without claiming to have read them."""

from typing import Protocol

from app.application.tool_context import ToolExecutionContext
from app.domain.search import SearchResult


class SearchProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def key_hash(self) -> str | None: ...

    async def search(
        self, query: str, max_results: int, context: ToolExecutionContext
    ) -> SearchResult: ...
