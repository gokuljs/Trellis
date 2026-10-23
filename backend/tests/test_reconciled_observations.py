import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from run_fixtures import run_record
from test_tool_dispatch import tool_record
from test_workspace_execution import execution

from app.application.errors import ApplicationError
from app.domain.runtime import ToolCallStatus, ToolResult
from app.infrastructure.local_patch import LocalPatchToolExecutor
from app.infrastructure.local_tools import LocalReadToolExecutor


@pytest.mark.parametrize("change_other", [False, True])
def test_reconciled_patch_refreshes_own_target_without_masking_unrelated_changes(
    tmp_path, change_other
):
    owner, root, _ = execution(tmp_path)
    (root / "hello.txt").write_text("hello world\n")
    (root / "other.txt").write_text("Original unrelated input.\n")
    run = run_record()

    async def exercise():
        previous = []
        for name in ("hello.txt", "other.txt"):
            tool = replace(
                tool_record("read_file", {"path": name, "start_line": 1, "max_lines": 5}),
                id=str(uuid4()),
            )
            async with owner.operation(run, tool, root, previous) as operation:
                await operation.capture_inputs()
                content, truncated = await LocalReadToolExecutor().execute(
                    tool.name, tool.arguments, root
                )
                result = await operation.capture_result(
                    ToolResult(tool.provider_call_id, tool.name, content, truncated=truncated)
                )
            previous.append(replace(tool, status=ToolCallStatus.COMPLETED, result_snapshot=result))
        arguments = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
        patch = LocalPatchToolExecutor()
        preview = await patch.approval_preview(arguments, root)
        tool = replace(
            tool_record("apply_patch", arguments, approved=True),
            approval_preview=preview,
            status=ToolCallStatus.RUNNING,
            dispatch_attempt=1,
            dispatch_id=str(uuid4()),
        )
        await patch.execute(arguments, root, preview)
        if change_other:
            (root / "other.txt").write_text("Externally changed unrelated input.\n")
        async with owner.operation(run, tool, root, previous) as operation:
            assert (await operation.reconcile_patch()).state == "desired_state_present"
            if change_other:
                with pytest.raises(ApplicationError) as failure:
                    await operation.capture_result(
                        ToolResult(tool.provider_call_id, tool.name, "Updated hello.txt.")
                    )
                assert failure.value.code == "workspace_inputs_changed"
            else:
                saved = await operation.capture_result(
                    ToolResult(tool.provider_call_id, tool.name, "Updated hello.txt.")
                )
                assert (
                    saved["workspace_observations"]
                    != previous[-1].result_snapshot["workspace_observations"]
                )
        assert (root / "hello.txt").read_text() == "goodbye world\n"

    asyncio.run(exercise())
