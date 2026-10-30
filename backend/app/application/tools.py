"""Provider-neutral tool registration, validation, and dispatch."""

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.application.errors import ApplicationError
from app.application.secret_redaction import COMMON_SECRET_REDACTOR, SecretRedactor
from app.application.tool_context import ToolExecutionContext
from app.domain.capabilities import (
    DEFAULT_RUNTIME_CAPABILITIES,
    RuntimeCapabilities,
    RuntimeCapability,
)
from app.domain.recovery import ToolRecoveryPolicy
from app.domain.runtime import ModelToolCall, ModelToolSpec, ToolResult

MAX_ARGUMENT_BYTES = 16_384
MAX_RESULT_BYTES = 20_000
MAX_COMMAND_BYTES = 2_048
MAX_COMMAND_ARGUMENTS = 64


def redact_secrets(content: str) -> str:
    return COMMON_SECRET_REDACTOR.redact_text(content)


def redact_record(value: object) -> object:
    return COMMON_SECRET_REDACTOR.redact_record(value)


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


class _ApplyPatchArguments(_StrictArguments):
    path: str = Field(min_length=1, max_length=4096)
    old_text: str = Field(max_length=8192)
    new_text: str = Field(max_length=8192)


class _RunCommandArguments(_StrictArguments):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_BYTES)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=120, ge=1, le=120)


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
_PATCH_DEFINITION = (
    "apply_patch",
    "Create or replace exact text in one workspace file after a diff preview and user approval.",
    _ApplyPatchArguments,
)

_COMMAND_DEFINITIONS: tuple[tuple[str, str, type[_StrictArguments]], ...] = (
    (
        "run_command",
        "Run one executable from a workspace folder after approval, using Trellis's OS "
        "permissions. Shell operators are unavailable.",
        _RunCommandArguments,
    ),
)

_LOCAL_CAPABILITIES: dict[str, RuntimeCapability] = {
    "list_files": "local_read",
    "search_files": "local_read",
    "read_file": "local_read",
    "inspect_git": "local_read",
    "apply_patch": "local_patch",
    "run_command": "local_command",
}


class ReadToolExecutor(Protocol):
    async def execute(
        self, name: str, arguments: dict[str, object], workspace_root: Path
    ) -> tuple[str, bool]: ...


class PatchToolExecutor(Protocol):
    async def approval_preview(
        self, arguments: dict[str, object], workspace_root: Path
    ) -> dict[str, object]: ...

    async def execute(
        self,
        arguments: dict[str, object],
        workspace_root: Path,
        approval_preview: dict[str, object] | None,
    ) -> tuple[str, bool]: ...


class CommandToolExecutor(Protocol):
    async def execute(
        self, name: str, arguments: dict[str, object], workspace_root: Path
    ) -> tuple[str, bool]: ...


class ToolExecutor(Protocol):
    async def execute(
        self, name: str, arguments: dict[str, object], context: ToolExecutionContext
    ) -> tuple[str, bool]: ...


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    name: str
    description: str
    arguments_model: type[BaseModel]
    executor: ToolExecutor
    required_capabilities: frozenset[RuntimeCapability] = frozenset()
    requires_workspace: bool = False
    recovery_policy: ToolRecoveryPolicy = ToolRecoveryPolicy.NEVER
    recovery_version: str = "v1"


@dataclass(frozen=True, slots=True)
class _ToolRequirements:
    required_capabilities: frozenset[RuntimeCapability]
    requires_workspace: bool


class ToolExecutionError(Exception):
    """A safe error code and message that may be returned to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_command(command: str) -> tuple[str, ...]:
    """Accept one executable and arguments; never invoke a shell."""
    if (
        not command
        or len(command.encode("utf-8")) > MAX_COMMAND_BYTES
        or any(character in command for character in ("\x00", "\r", "\n"))
    ):
        raise ToolExecutionError("invalid_command", "Enter one bounded command.")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        arguments = tuple(lexer)
    except ValueError:
        raise ToolExecutionError("invalid_command", "The command syntax is invalid.") from None
    if (
        not arguments
        or len(arguments) > MAX_COMMAND_ARGUMENTS
        or any(argument and set(argument) <= set("|&;<>()") for argument in arguments)
    ):
        raise ToolExecutionError("invalid_command", "Enter one executable without shell operators.")
    return arguments


class ToolRegistry:
    def __init__(
        self,
        executor: ReadToolExecutor | None = None,
        *,
        patch_executor: PatchToolExecutor | None = None,
        command_executor: CommandToolExecutor | None = None,
        approval_required_names: frozenset[str] | set[str] = frozenset(),
        capabilities: RuntimeCapabilities = DEFAULT_RUNTIME_CAPABILITIES,
        additional_tools: tuple[ToolRegistration, ...] = (),
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._executor = executor
        self._capabilities = capabilities
        self._redactor = redactor or COMMON_SECRET_REDACTOR
        self._patch_executor = patch_executor
        self._command_executor = command_executor
        self._generic_executors: dict[str, ToolExecutor] = {}
        self._recovery_versions = {name: "v1" for name in _LOCAL_CAPABILITIES}
        self._recovery_policies = {
            **{name: ToolRecoveryPolicy.READ_ONLY for name, _description, _model in _DEFINITIONS},
            "apply_patch": ToolRecoveryPolicy.PATCH,
            "run_command": ToolRecoveryPolicy.COMMAND,
        }
        self._requirements = {
            name: _ToolRequirements(frozenset({capability}), True)
            for name, capability in _LOCAL_CAPABILITIES.items()
        }
        for tool in additional_tools:
            if not tool.name or tool.name in self._requirements:
                raise ValueError("Tool names must be nonempty and unique")
            if not tool.recovery_version or len(tool.recovery_version) > 100:
                raise ValueError("Tools require a bounded recovery version")
            self._recovery_versions[tool.name] = tool.recovery_version
            self._generic_executors[tool.name] = tool.executor
            self._recovery_policies[tool.name] = tool.recovery_policy
            self._requirements[tool.name] = _ToolRequirements(
                tool.required_capabilities, tool.requires_workspace
            )
        self._available_definitions: tuple[tuple[str, str, type[BaseModel]], ...] = (
            *(_DEFINITIONS if executor is not None else ()),
            *((_PATCH_DEFINITION,) if patch_executor is not None else ()),
            *(_COMMAND_DEFINITIONS if command_executor is not None else ()),
            *((tool.name, tool.description, tool.arguments_model) for tool in additional_tools),
        )
        self._definitions = {
            name: model for name, _description, model in self._available_definitions
        }
        self._approval_required_names = (
            frozenset(approval_required_names)
            | ({"apply_patch"} if patch_executor is not None else set())
            | ({"run_command"} if command_executor is not None else set())
        )

    def requires_approval(self, call: ModelToolCall, workspace_root: Path | None = None) -> bool:
        del workspace_root
        return call.name in self._approval_required_names

    def recovery_policy(self, name: str) -> ToolRecoveryPolicy:
        return self._recovery_policies.get(name, ToolRecoveryPolicy.NEVER)

    def requires_workspace(self, name: str) -> bool:
        requirements = self._requirements.get(name)
        return requirements is not None and requirements.requires_workspace

    def recovery_versions(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (name, self._recovery_versions[name])
                for name in self._definitions
                if self._capabilities.allows_all(self._requirements[name].required_capabilities)
            )
        )

    def validate_execution(
        self,
        call: ModelToolCall,
        workspace_root: Path | None,
        *,
        approved: bool = False,
        approval_preview: dict[str, object] | None = None,
        capabilities: RuntimeCapabilities | None = None,
    ) -> dict[str, object]:
        self.check_call(call, workspace_root, capabilities=capabilities)
        if self.requires_approval(call, workspace_root) and not approved:
            raise ToolExecutionError(
                "approval_required", "This tool requires approval before it can run."
            )
        arguments = self._validated_arguments(call)
        if call.name == "run_command":
            parse_command(str(arguments["command"]))
            if approval_preview != {
                "command": arguments["command"],
                "cwd": arguments["cwd"],
                "timeout_seconds": arguments["timeout_seconds"],
            }:
                raise ToolExecutionError(
                    "approval_changed", "The approved command no longer matches this request."
                )
        return arguments

    async def approval_preview(
        self,
        call: ModelToolCall,
        workspace_root: Path | None,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> dict[str, object] | None:
        self.check_call(call, workspace_root, capabilities=capabilities)
        if workspace_root is None:
            return None
        if call.name == "apply_patch" and self._patch_executor is not None:
            return await self._patch_executor.approval_preview(
                self._validated_arguments(call), workspace_root
            )
        if call.name == "run_command" and self._command_executor is not None:
            arguments = self._validated_arguments(call)
            parse_command(str(arguments["command"]))
            return {
                "command": arguments["command"],
                "cwd": arguments["cwd"],
                "timeout_seconds": arguments["timeout_seconds"],
            }
        return None

    def check_capabilities(
        self,
        call: ModelToolCall,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        requirements = self._requirements.get(call.name)
        if requirements is None:
            raise ToolExecutionError("unknown_tool", "This tool is not available.")
        if not self._capabilities.allows_all(requirements.required_capabilities) or (
            capabilities is not None
            and not capabilities.allows_all(requirements.required_capabilities)
        ):
            raise ToolExecutionError(
                "capability_disabled", "This tool is disabled by the operator."
            )
        if call.name not in self._definitions:
            raise ToolExecutionError("unknown_tool", "This tool is not available.")

    def check_call(
        self,
        call: ModelToolCall,
        workspace_root: Path | None,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> None:
        self.check_capabilities(call, capabilities=capabilities)
        if self._requirements[call.name].requires_workspace and workspace_root is None:
            raise ToolExecutionError(
                "workspace_required", "This tool requires a selected workspace."
            )

    def specs(
        self,
        workspace_root: Path | None = None,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> tuple[ModelToolSpec, ...]:
        return tuple(
            ModelToolSpec(name, description, model.model_json_schema())
            for name, description, model in self._available_definitions
            if (not self._requirements[name].requires_workspace or workspace_root is not None)
            and self._capabilities.allows_all(self._requirements[name].required_capabilities)
            and (
                capabilities is None
                or capabilities.allows_all(self._requirements[name].required_capabilities)
            )
        )

    def _validated_arguments(self, call: ModelToolCall) -> dict[str, object]:
        model = self._definitions.get(call.name)
        if model is None:
            raise ToolExecutionError("unknown_tool", "This tool is not available.")
        try:
            encoded = json.dumps(call.arguments, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise ValueError("tool arguments exceed the size limit")
            return model.model_validate(call.arguments).model_dump()
        except TypeError, ValueError, ValidationError:
            raise ToolExecutionError(
                "invalid_tool_arguments", "Tool arguments are invalid."
            ) from None

    def _safe_error(self, call: ModelToolCall, error: ToolExecutionError) -> ToolResult:
        content = self._redactor.redact_text(error.message)
        encoded = content.encode("utf-8")
        truncated = len(encoded) > MAX_RESULT_BYTES
        if truncated:
            content = encoded[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
        return ToolResult(
            call.id, call.name, content, True, self._redactor.redact_text(error.code), truncated
        )

    async def execute(
        self,
        call: ModelToolCall,
        workspace_root: Path | None,
        *,
        approved: bool = False,
        approval_preview: dict[str, object] | None = None,
        capabilities: RuntimeCapabilities | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            arguments = self.validate_execution(
                call,
                workspace_root,
                approved=approved,
                approval_preview=approval_preview,
                capabilities=capabilities,
            )
        except ToolExecutionError as error:
            return self._safe_error(call, error)
        try:
            generic_executor = self._generic_executors.get(call.name)
            if generic_executor is not None:
                if context is None:
                    raise ToolExecutionError(
                        "tool_context_required", "This tool requires a trusted run context."
                    )
                await context.assert_active()
                content, truncated = await generic_executor.execute(call.name, arguments, context)
            elif workspace_root is None:
                raise ToolExecutionError(
                    "workspace_required", "This tool requires a selected workspace."
                )
            elif call.name == "apply_patch":
                if self._patch_executor is None:
                    raise ToolExecutionError("unknown_tool", "This tool is not available.")
                content, truncated = await self._patch_executor.execute(
                    arguments, workspace_root, approval_preview
                )
            elif call.name == "run_command":
                if self._command_executor is None:
                    raise ToolExecutionError("unknown_tool", "This tool is not available.")
                content, truncated = await self._command_executor.execute(
                    call.name, arguments, workspace_root
                )
            else:
                if self._executor is None:
                    raise ToolExecutionError("unknown_tool", "This tool is not available.")
                content, truncated = await self._executor.execute(
                    call.name, arguments, workspace_root
                )
        except ToolExecutionError as error:
            return self._safe_error(call, error)
        except ApplicationError, TimeoutError:
            raise
        except Exception:
            if self.recovery_policy(call.name) is not ToolRecoveryPolicy.READ_ONLY:
                raise
            return ToolResult(
                call.id, call.name, "The tool could not complete.", True, "tool_failed"
            )
        safe_content = self._redactor.redact_text(content)
        encoded = safe_content.encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            safe_content = encoded[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
            truncated = True
        return ToolResult(call.id, call.name, safe_content, truncated=truncated)
