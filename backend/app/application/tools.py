"""Provider-neutral definitions and validation for local read tools."""

import json
import re
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.runtime import ModelToolCall, ModelToolSpec, ToolResult

MAX_ARGUMENT_BYTES = 16_384
MAX_RESULT_BYTES = 20_000
_BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/-]{6,})")
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?)([^\s\"';,}]+)"
)
_STANDALONE_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,})\b"
)


def redact_secrets(content: str) -> str:
    content = _BEARER_TOKEN.sub(lambda match: f"{match[1]}[REDACTED]", content)
    content = _KEY_ASSIGNMENT.sub(lambda match: f"{match[1]}[REDACTED]", content)
    return _STANDALONE_TOKEN.sub("[REDACTED]", content)


def redact_record(value: object) -> object:
    """Redact common credentials in nested model and tool records."""
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.casefold().replace("-", "_")
            sensitive = normalized in {"token", "secret", "password", "credential", "api_key"} or (
                normalized.endswith(("_token", "_secret", "_password", "_credential", "_api_key"))
            )
            redacted[key] = "[REDACTED]" if sensitive else redact_record(item)
        return redacted
    if isinstance(value, list | tuple):
        return [redact_record(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ListFilesArguments(_StrictArguments):
    path: str = Field(default=".", min_length=1, max_length=4096)
    limit: int = Field(default=100, ge=1, le=200)


class _SearchFilesArguments(_StrictArguments):
    query: str = Field(min_length=1, max_length=256)
    path: str = Field(default=".", min_length=1, max_length=4096)
    limit: int = Field(default=50, ge=1, le=100)
    case_sensitive: bool = True


class _ReadFileArguments(_StrictArguments):
    path: str = Field(min_length=1, max_length=4096)
    start_line: int = Field(default=1, ge=1, le=10_000)
    max_lines: int = Field(default=200, ge=1, le=200)


class _InspectGitArguments(_StrictArguments):
    operation: Literal["status", "diff", "log"]


_DEFINITIONS: tuple[tuple[str, str, type[_StrictArguments]], ...] = (
    (
        "list_files",
        "List up to 200 workspace file paths recursively, excluding protected and generated files.",
        _ListFilesArguments,
    ),
    (
        "search_files",
        "Find literal text in workspace files and return bounded line matches.",
        _SearchFilesArguments,
    ),
    (
        "read_file",
        "Read a bounded line range from a UTF-8 workspace file.",
        _ReadFileArguments,
    ),
    (
        "inspect_git",
        "Inspect Git status, unstaged diff, or recent commits at the workspace root.",
        _InspectGitArguments,
    ),
)


class ReadToolExecutor(Protocol):
    async def execute(
        self, name: str, arguments: dict[str, object], workspace_root: Path
    ) -> tuple[str, bool]: ...


class ToolExecutionError(Exception):
    """A safe error code and message that may be returned to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolRegistry:
    def __init__(
        self,
        executor: ReadToolExecutor,
        *,
        approval_required_names: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self._executor = executor
        self._definitions = {name: model for name, _description, model in _DEFINITIONS}
        self._approval_required_names = frozenset(approval_required_names)

    def requires_approval(self, call: ModelToolCall, workspace_root: Path | None = None) -> bool:
        del workspace_root
        return call.name in self._approval_required_names

    async def approval_preview(
        self, call: ModelToolCall, workspace_root: Path
    ) -> dict[str, object] | None:
        del call, workspace_root
        return None

    def specs(self) -> tuple[ModelToolSpec, ...]:
        return tuple(
            ModelToolSpec(name, description, model.model_json_schema())
            for name, description, model in _DEFINITIONS
        )

    async def execute(
        self,
        call: ModelToolCall,
        workspace_root: Path,
        *,
        approved: bool = False,
        approval_preview: dict[str, object] | None = None,
    ) -> ToolResult:
        del approval_preview
        if self.requires_approval(call, workspace_root) and not approved:
            return ToolResult(
                call.id,
                call.name,
                "This tool requires approval before it can run.",
                True,
                "approval_required",
            )
        model = self._definitions.get(call.name)
        if model is None:
            return ToolResult(
                call.id, call.name, "This tool is not available.", True, "unknown_tool"
            )
        try:
            encoded = json.dumps(call.arguments, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise ValueError("tool arguments exceed the size limit")
            arguments = model.model_validate(call.arguments).model_dump()
        except TypeError, ValueError, ValidationError:
            return ToolResult(
                call.id, call.name, "Tool arguments are invalid.", True, "invalid_tool_arguments"
            )
        try:
            content, truncated = await self._executor.execute(call.name, arguments, workspace_root)
        except ToolExecutionError as error:
            return ToolResult(call.id, call.name, error.message, True, error.code)
        except Exception:
            return ToolResult(
                call.id, call.name, "The tool could not complete.", True, "tool_failed"
            )
        safe_content = redact_secrets(content)
        encoded = safe_content.encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            safe_content = encoded[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
            truncated = True
        return ToolResult(call.id, call.name, safe_content, truncated=truncated)
