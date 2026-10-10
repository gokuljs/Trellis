import asyncio
import os
import stat
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.models import ProviderName
from app.domain.runtime import (
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    RunEventType,
    ToolCallStatus,
)
from tests.memory_repository import MemoryRepository
from tests.support import TestClient, account_database_path, create_app


def test_attached_workspace_offers_patch_tool_to_the_model(tmp_path: Path) -> None:
    class CaptureProvider:
        name: ProviderName = "openai"
        model = "capture"

        def __init__(self) -> None:
            self.request: ModelRequest | None = None

        async def complete(self, *_args: object, **_kwargs: object) -> str:
            raise NotImplementedError

        def stream(
            self, request: ModelRequest, _api_key: str, _user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            self.request = request

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                yield ModelStreamEvent(kind="text_delta", text="Ready")
                yield ModelStreamEvent(kind="usage", input_tokens=1, output_tokens=1)
                yield ModelStreamEvent(kind="completed", finish_reason="stop")

            return generate()

    provider = CaptureProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(
        Settings(environment="test", data_dir=tmp_path / "data"),
        provider_adapters={"openai": provider},
    )
    with TestClient(app) as client:
        client.put("/api/settings/providers/openai/api-key", json={"api_key": "sk-patch-test"})
        session = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start-patch-catalog",
                    "method": "run.start",
                    "params": {
                        "sessionId": session["id"],
                        "clientRequestId": "patch-catalog",
                        "content": "What tools are available?",
                    },
                }
            )
            for _ in range(20):
                event = websocket.receive_json()
                if (
                    event.get("method") == "run.event"
                    and event["params"]["eventType"] == "run.completed"
                ):
                    break
            else:
                pytest.fail("run did not complete")

    assert provider.request is not None
    assert "apply_patch" in {tool.name for tool in provider.request.tools}


def test_approved_patch_uses_the_saved_preview_then_continues_the_run(tmp_path: Path) -> None:
    class PatchProvider:
        name: ProviderName = "openai"
        model = "patch-provider"

        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, *_args: object, **_kwargs: object) -> str:
            raise NotImplementedError

        def stream(
            self, request: ModelRequest, _api_key: str, _user_id: str
        ) -> AsyncGenerator[ModelStreamEvent]:
            self.requests.append(request)
            step = len(self.requests)

            async def generate() -> AsyncGenerator[ModelStreamEvent]:
                if step == 1:
                    yield ModelStreamEvent(
                        kind="tool_call",
                        tool_call=ModelToolCall(
                            "provider-edit",
                            "apply_patch",
                            {
                                "path": "src/note.txt",
                                "old_text": "before\n",
                                "new_text": "after\n",
                            },
                        ),
                    )
                    yield ModelStreamEvent(kind="usage", input_tokens=10, output_tokens=5)
                    yield ModelStreamEvent(kind="completed", finish_reason="tool_use")
                else:
                    yield ModelStreamEvent(kind="text_delta", text="The note was updated.")
                    yield ModelStreamEvent(kind="usage", input_tokens=10, output_tokens=5)
                    yield ModelStreamEvent(kind="completed", finish_reason="stop")

            return generate()

    workspace = tmp_path / "project"
    source = workspace / "src"
    source.mkdir(parents=True)
    target = source / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("Review the diff.\n", encoding="utf-8")
    provider = PatchProvider()
    settings = Settings(environment="test", data_dir=tmp_path / "data")
    app = create_app(settings, provider_adapters={"openai": provider})

    with TestClient(app) as client:
        client.put("/api/settings/providers/openai/api-key", json={"api_key": "sk-patch-test"})
        session_id = client.post("/api/sessions", json={"workspace_path": str(workspace)}).json()[
            "id"
        ]
        with client.websocket_connect("/api/runtime") as websocket:
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "start-patch",
                    "method": "run.start",
                    "params": {
                        "sessionId": session_id,
                        "clientRequestId": "patch-request",
                        "content": "Update the note",
                    },
                }
            )
            waiting = []
            while True:
                item = websocket.receive_json()
                waiting.append(item)
                if item.get("method") == "run.event" and item["params"]["eventType"] in {
                    RunEventType.TOOL_APPROVAL_REQUESTED.value,
                    RunEventType.FAILED.value,
                }:
                    break
            request = waiting[-1]["params"]
            assert request["eventType"] == RunEventType.TOOL_APPROVAL_REQUESTED.value
            run_id = next(
                item["result"]["runId"] for item in waiting if item.get("id") == "start-patch"
            )
            tool_call_id = request["data"]["tool_call_id"]
            preview = request["data"]["preview"]
            assert "-before" in preview["diff"]
            assert "+after" in preview["diff"]
            assert preview["guidance"] == [
                {"path": "src/AGENTS.md", "content": "Review the diff.\n"}
            ]
            assert target.read_text(encoding="utf-8") == "before\n"

            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": "approve-patch",
                    "method": "run.respond",
                    "params": {
                        "runId": run_id,
                        "toolCallId": tool_call_id,
                        "decision": "approved",
                    },
                }
            )
            resumed = []
            while True:
                item = websocket.receive_json()
                resumed.append(item)
                if item.get("method") == "run.event" and item["params"]["eventType"] in {
                    RunEventType.COMPLETED.value,
                    RunEventType.FAILED.value,
                }:
                    break
        visible = client.get(f"/api/sessions/{session_id}").json()["messages"]

    assert resumed[-1]["params"]["eventType"] == RunEventType.COMPLETED.value
    assert target.read_text(encoding="utf-8") == "after\n"
    assert [message["content"] for message in visible] == [
        "Update the note",
        "The note was updated.",
    ]
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].content == "Updated src/note.txt."
    saved_call = MemoryRepository(account_database_path(settings)).state.tool_calls[tool_call_id]
    assert saved_call.status is ToolCallStatus.COMPLETED
    assert saved_call.approval_preview is not None
    assert saved_call.approval_preview["target_sha256"] == preview["target_sha256"]


def test_patch_tool_is_offered_with_strict_args_and_requires_approved_preview(
    tmp_path: Path,
) -> None:
    from app.application.tools import ToolRegistry
    from app.infrastructure.local_patch import LocalPatchToolExecutor
    from app.infrastructure.local_tools import LocalReadToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    registry = ToolRegistry(
        LocalReadToolExecutor(),
        patch_executor=LocalPatchToolExecutor(),
        approval_required_names={"apply_patch"},
    )
    arguments: dict[str, object] = {
        "path": "note.txt",
        "old_text": "before",
        "new_text": "after",
    }
    call = ModelToolCall("edit-1", "apply_patch", arguments)

    spec = next(spec for spec in registry.specs() if spec.name == "apply_patch")
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["required"] == ["path", "old_text", "new_text"]
    assert registry.requires_approval(call)
    preview = asyncio.run(registry.approval_preview(call, tmp_path))
    assert preview is not None
    blocked = asyncio.run(registry.execute(call, tmp_path))
    assert blocked.error_code == "approval_required"
    assert target.read_text(encoding="utf-8") == "before\n"

    result = asyncio.run(registry.execute(call, tmp_path, approved=True, approval_preview=preview))
    assert not result.is_error
    assert result.content == "Updated note.txt."
    assert target.read_text(encoding="utf-8") == "after\n"


def test_patch_preview_and_approved_apply_edit_only_the_exact_target(tmp_path: Path) -> None:
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "src" / "hello.py"
    target.parent.mkdir()
    target.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    executor = LocalPatchToolExecutor()
    arguments = {"path": "src/hello.py", "old_text": "second = 2", "new_text": "second = 3"}

    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))

    assert target.read_text(encoding="utf-8") == "first = 1\nsecond = 2\n"
    assert preview["path"] == "src/hello.py"
    assert isinstance(preview["diff"], str)
    assert "-second = 2" in preview["diff"]
    assert "+second = 3" in preview["diff"]
    assert isinstance(preview["target_sha256"], str)
    assert asyncio.run(executor.execute(arguments, tmp_path, preview)) == (
        "Updated src/hello.py.",
        False,
    )
    assert target.read_text(encoding="utf-8") == "first = 1\nsecond = 3\n"


def test_patch_preview_marks_missing_final_newlines_clearly(tmp_path: Path) -> None:
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    arguments = {"path": "note.txt", "old_text": "before", "new_text": "after"}

    preview = asyncio.run(LocalPatchToolExecutor().approval_preview(arguments, tmp_path))

    assert preview["diff"] == (
        "--- a/note.txt\n"
        "+++ b/note.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "\\ No newline at end of file\n"
        "+after\n"
        "\\ No newline at end of file\n"
    )


def test_patch_rejects_a_stale_approved_preview(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("top\nchange me\n", encoding="utf-8")
    executor = LocalPatchToolExecutor()
    arguments = {"path": "note.txt", "old_text": "change me", "new_text": "changed"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    target.write_text("new first line\nchange me\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(arguments, tmp_path, preview))

    assert error.value.code == "target_changed"
    assert target.read_text(encoding="utf-8") == "new first line\nchange me\n"


def test_patch_rejects_a_replaced_target_even_when_text_is_unchanged(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    arguments = {"path": "note.txt", "old_text": "before", "new_text": "after"}
    executor = LocalPatchToolExecutor()
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("before\n", encoding="utf-8")
    replacement.replace(target)

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(arguments, tmp_path, preview))

    assert error.value.code == "target_changed"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_rejects_a_preview_whose_diff_does_not_match_the_edit(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    arguments = {"path": "note.txt", "old_text": "before", "new_text": "after"}
    executor = LocalPatchToolExecutor()
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    preview["diff"] = "No changes will be made."

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(arguments, tmp_path, preview))

    assert error.value.code == "approval_invalid"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_rejects_ambiguous_text_and_outside_or_instruction_paths(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    (tmp_path / "duplicate.txt").write_text("same\nsame\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("instructions", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    executor = LocalPatchToolExecutor()

    for path, old_text, expected_code in (
        ("duplicate.txt", "same", "ambiguous_patch"),
        ("../outside.txt", "outside", "path_not_allowed"),
        ("link.txt", "outside", "path_not_allowed"),
        ("AGENTS.md", "instructions", "path_not_allowed"),
    ):
        with pytest.raises(ToolExecutionError) as error:
            asyncio.run(
                executor.approval_preview(
                    {"path": path, "old_text": old_text, "new_text": "changed"}, tmp_path
                )
            )
        assert error.value.code == expected_code
    assert outside.read_text(encoding="utf-8") == "outside"


def test_patch_preview_returns_a_safe_error_for_an_invalid_filename(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(
            LocalPatchToolExecutor().approval_preview(
                {"path": "x" * 300, "old_text": "before", "new_text": "after"},
                tmp_path,
            )
        )

    assert error.value.code == "path_not_allowed"


@pytest.mark.parametrize(
    ("path", "new_text"),
    [
        ("note.txt", "after\x00binary"),
        ("bad\ud800name.txt", "after"),
        ("note.txt", "bad\ud800text"),
    ],
)
def test_patch_rejects_binary_or_invalid_unicode_arguments_before_preview(
    tmp_path: Path, path: str, new_text: str
) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(
            LocalPatchToolExecutor().approval_preview(
                {"path": path, "old_text": "before", "new_text": new_text},
                tmp_path,
            )
        )

    assert error.value.code == "invalid_tool_arguments"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_preview_carries_bounded_ancestor_guidance(tmp_path: Path) -> None:
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    (tmp_path / "AGENTS.md").write_text("Root rules.\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "AGENTS.md").write_text("Use tests first.\n", encoding="utf-8")
    (source / "note.txt").write_text("before\n", encoding="utf-8")

    preview = asyncio.run(
        LocalPatchToolExecutor().approval_preview(
            {"path": "src/note.txt", "old_text": "before", "new_text": "after"}, tmp_path
        )
    )

    assert preview["guidance"] == [
        {"path": "AGENTS.md", "content": "Root rules.\n"},
        {"path": "src/AGENTS.md", "content": "Use tests first.\n"},
    ]


def test_patch_stops_if_guidance_changes_while_preparing_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure import local_patch

    guidance = tmp_path / "AGENTS.md"
    guidance.write_text("Original rules.\n", encoding="utf-8")
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    executor = local_patch.LocalPatchToolExecutor()
    arguments = {"path": "note.txt", "old_text": "before", "new_text": "after"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    write_all = local_patch._write_all

    def change_guidance_after_write(descriptor: int, data: bytes) -> None:
        write_all(descriptor, data)
        guidance.write_text("New rules.\n", encoding="utf-8")

    monkeypatch.setattr(local_patch, "_write_all", change_guidance_after_write)

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(arguments, tmp_path, preview))

    assert error.value.code == "target_changed"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_patch_stops_if_parent_directory_is_replaced_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure import local_patch

    source = tmp_path / "src"
    source.mkdir()
    (source / "note.txt").write_text("before\n", encoding="utf-8")
    moved = tmp_path / "moved"
    executor = local_patch.LocalPatchToolExecutor()
    arguments = {"path": "src/note.txt", "old_text": "before", "new_text": "after"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    write_all = local_patch._write_all

    def replace_directory_after_write(descriptor: int, data: bytes) -> None:
        write_all(descriptor, data)
        source.rename(moved)
        source.mkdir()
        (source / "note.txt").write_text("before\n", encoding="utf-8")

    monkeypatch.setattr(local_patch, "_write_all", replace_directory_after_write)

    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(arguments, tmp_path, preview))

    assert error.value.code == "target_changed"
    assert (moved / "note.txt").read_text(encoding="utf-8") == "before\n"
    assert (source / "note.txt").read_text(encoding="utf-8") == "before\n"


def test_patch_can_create_a_file_only_if_it_is_still_absent(tmp_path: Path) -> None:
    from app.application.tools import ToolExecutionError
    from app.infrastructure.local_patch import LocalPatchToolExecutor

    executor = LocalPatchToolExecutor()
    arguments = {"path": "created.py", "old_text": "", "new_text": "print('hello')\n"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))

    assert preview["target_sha256"] is None
    assert isinstance(preview["diff"], str)
    assert "--- /dev/null" in preview["diff"]
    assert "+print('hello')" in preview["diff"]
    assert asyncio.run(executor.execute(arguments, tmp_path, preview)) == (
        "Created created.py.",
        False,
    )
    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print('hello')\n"

    second = {"path": "later.py", "old_text": "", "new_text": "approved\n"}
    stale = asyncio.run(executor.approval_preview(second, tmp_path))
    (tmp_path / "later.py").write_text("someone else's file\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(executor.execute(second, tmp_path, stale))
    assert error.value.code == "target_changed"
    assert (tmp_path / "later.py").read_text(encoding="utf-8") == "someone else's file\n"


def test_patch_reports_applied_edit_even_if_directory_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure import local_patch

    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    executor = local_patch.LocalPatchToolExecutor()
    arguments = {"path": "note.txt", "old_text": "before", "new_text": "after"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory sync failure")
        fsync(descriptor)

    monkeypatch.setattr(local_patch.os, "fsync", fail_directory_sync)

    assert asyncio.run(executor.execute(arguments, tmp_path, preview)) == (
        "Updated note.txt.",
        False,
    )
    assert target.read_text(encoding="utf-8") == "after\n"


def test_created_file_reports_success_if_temporary_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure import local_patch

    executor = local_patch.LocalPatchToolExecutor()
    arguments = {"path": "new.txt", "old_text": "", "new_text": "content\n"}
    preview = asyncio.run(executor.approval_preview(arguments, tmp_path))
    unlink = os.unlink

    def fail_temporary_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path.startswith(".trellis-patch-"):
            raise OSError("simulated cleanup failure")
        unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(local_patch.os, "unlink", fail_temporary_unlink)

    assert asyncio.run(executor.execute(arguments, tmp_path, preview)) == (
        "Created new.txt.",
        False,
    )
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "content\n"


def test_temporary_patch_files_are_hidden_from_read_tools(tmp_path: Path) -> None:
    from app.application.tools import ToolRegistry
    from app.infrastructure.local_tools import LocalReadToolExecutor

    (tmp_path / ".trellis-patch-incomplete").write_text("private candidate", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("public", encoding="utf-8")
    registry = ToolRegistry(LocalReadToolExecutor())

    listed = asyncio.run(registry.execute(ModelToolCall("list", "list_files", {}), tmp_path))
    searched = asyncio.run(
        registry.execute(
            ModelToolCall("search", "search_files", {"query": "private candidate"}), tmp_path
        )
    )

    assert listed.content == "visible.txt"
    assert searched.content == ""
