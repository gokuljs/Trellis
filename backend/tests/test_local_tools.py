import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from app.domain.runtime import ModelToolCall


def run_tool(root: Path, name: str, **arguments: object):
    from app.application.tools import ToolRegistry
    from app.infrastructure.local_tools import LocalReadToolExecutor

    registry = ToolRegistry(LocalReadToolExecutor())
    return asyncio.run(
        registry.execute(ModelToolCall(id="call-1", name=name, arguments=arguments), root)
    )


def test_approval_required_tool_refuses_execution_without_a_recorded_grant(
    tmp_path: Path,
) -> None:
    from app.application.tools import ToolRegistry
    from app.infrastructure.local_tools import LocalReadToolExecutor

    (tmp_path / "note.txt").write_text("safe\n", encoding="utf-8")
    registry = ToolRegistry(LocalReadToolExecutor(), approval_required_names={"read_file"})
    call = ModelToolCall("call-approval", "read_file", {"path": "note.txt"})

    async def check() -> None:
        assert registry.requires_approval(call)
        assert await registry.approval_preview(call, tmp_path) is None
        blocked = await registry.execute(call, tmp_path)
        assert blocked.error_code == "approval_required"
        granted = await registry.execute(call, tmp_path, approved=True)
        assert not granted.is_error
        assert "safe" in granted.content

    asyncio.run(check())


def test_read_tools_expose_the_same_strict_schemas_used_for_execution(tmp_path: Path) -> None:
    from app.application.tools import ToolRegistry
    from app.infrastructure.local_tools import LocalReadToolExecutor

    specs = {spec.name: spec for spec in ToolRegistry(LocalReadToolExecutor()).specs()}

    assert set(specs) == {"list_files", "search_files", "read_file", "inspect_git"}
    assert specs["read_file"].input_schema["additionalProperties"] is False
    assert specs["read_file"].input_schema["required"] == ["path"]

    invalid = run_tool(tmp_path, "read_file", path="note.txt", unknown=True)
    assert invalid.is_error
    assert invalid.error_code == "invalid_tool_arguments"


def test_read_file_returns_numbered_bounded_lines(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = run_tool(tmp_path, "read_file", path="notes.txt", start_line=2, max_lines=1)

    assert not result.is_error
    assert result.content == "2: second"
    assert result.truncated


def test_read_file_keeps_a_useful_prefix_when_one_line_exceeds_the_output_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "large.txt").write_text("a" * 25_000, encoding="utf-8")

    result = run_tool(tmp_path, "read_file", path="large.txt")

    assert result.content.startswith("1: aaaaa")
    assert len(result.content.encode("utf-8")) <= 20_000
    assert result.truncated


def test_read_file_refuses_outside_symlink_and_sensitive_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("private", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / ".env").write_text("API_KEY=private", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("//registry/:_authToken=private", encoding="utf-8")

    for path in (
        "../outside-secret.txt",
        str(outside),
        "link.txt",
        ".env",
        ".npmrc",
        ".git/config",
    ):
        result = run_tool(tmp_path, "read_file", path=path)
        assert result.is_error
        assert result.error_code == "path_not_allowed"
        assert "private" not in result.content


def test_read_file_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"data\x00more")
    (tmp_path / "large.txt").write_text("x" * 1_000_001, encoding="utf-8")

    binary = run_tool(tmp_path, "read_file", path="binary.bin")
    large = run_tool(tmp_path, "read_file", path="large.txt")

    assert binary.error_code == "not_text_file"
    assert large.error_code == "file_too_large"
    assert binary.is_error and large.is_error


def test_list_files_skips_sensitive_files_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('a')", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("print('b')", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("secret", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)

    result = run_tool(tmp_path, "list_files", path=".", limit=1)

    assert result.content == "src/a.py"
    assert result.truncated
    assert not result.is_error


def test_search_files_matches_text_and_skips_binary_and_secrets(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Alpha\nalpha\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"alpha\x00more")
    (tmp_path / ".env").write_text("alpha-secret", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("alpha-secret", encoding="utf-8")

    result = run_tool(tmp_path, "search_files", query="alpha", case_sensitive=False)

    assert result.content == "a.txt:1:Alpha\na.txt:2:alpha"
    assert not result.is_error


def test_search_files_keeps_a_match_visible_on_a_long_line(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("x" * 700 + "needle" + "y" * 100, encoding="utf-8")

    result = run_tool(tmp_path, "search_files", query="needle")

    assert "needle" in result.content
    assert len(result.content) < 300
    assert result.truncated


def test_search_files_reports_limited_or_skipped_results(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "large.txt").write_text("x" * 256_001 + "needle", encoding="utf-8")

    limited = run_tool(tmp_path, "search_files", query="needle", limit=1)
    skipped = run_tool(tmp_path, "search_files", query="no-match")

    assert limited.content == "a.txt:1:needle"
    assert limited.truncated
    assert skipped.content == ""
    assert skipped.truncated


def test_tool_results_redact_key_assignments_and_bearer_tokens(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text(
        "OPENAI_API_KEY=sk-test-secret123\nAuthorization: Bearer bearer-secret123\nordinary note\n",
        encoding="utf-8",
    )

    read = run_tool(tmp_path, "read_file", path="notes.txt")
    search = run_tool(tmp_path, "search_files", query="secret")

    for result in (read, search):
        assert not result.is_error
        assert "sk-test-secret123" not in result.content
        assert "bearer-secret123" not in result.content
        assert "[REDACTED]" in result.content
    assert "ordinary note" in read.content


def test_invalid_call_does_not_execute_and_errors_are_safe(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("secret", encoding="utf-8")

    unknown = run_tool(tmp_path, "not_registered", path="notes.txt")
    bad_count = run_tool(tmp_path, "read_file", path="notes.txt", max_lines=True)

    assert (unknown.error_code, bad_count.error_code) == (
        "unknown_tool",
        "invalid_tool_arguments",
    )
    assert "secret" not in unknown.content + bad_count.content


def test_unavailable_workspace_returns_safe_error(tmp_path: Path) -> None:
    result = run_tool(tmp_path / "missing", "list_files")

    assert result.error_code == "invalid_workspace"
    assert result.is_error


def test_read_file_rejects_invalid_paths_and_non_utf8_content(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "invalid.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "alias").symlink_to(tmp_path / "folder", target_is_directory=True)

    for path in (
        "C:\\outside.txt",
        "folder/../invalid.txt",
        "folder\x00/file",
        "alias/invalid.txt",
    ):
        result = run_tool(tmp_path, "read_file", path=path)
        assert result.error_code == "path_not_allowed"

    assert run_tool(tmp_path, "read_file", path="folder/invalid.txt").error_code == "not_text_file"
    assert (
        run_tool(tmp_path, "read_file", path="folder/missing.txt").error_code == "path_not_allowed"
    )
    assert run_tool(tmp_path, "read_file", path="folder").error_code == "not_text_file"
    assert run_tool(tmp_path / "alias", "list_files").error_code == "invalid_workspace"


def test_registry_rejects_oversized_arguments_and_masks_unexpected_errors(tmp_path: Path) -> None:
    from app.application.tools import ToolRegistry

    class FailingExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            raise RuntimeError("private executor detail")

    registry = ToolRegistry(FailingExecutor())
    oversized = asyncio.run(
        registry.execute(
            ModelToolCall(id="large", name="read_file", arguments={"path": "x" * 17_000}),
            tmp_path,
        )
    )
    failed = asyncio.run(
        registry.execute(
            ModelToolCall(id="failed", name="read_file", arguments={"path": "note.txt"}),
            tmp_path,
        )
    )

    assert oversized.error_code == "invalid_tool_arguments"
    assert failed.error_code == "tool_failed"
    assert "private executor detail" not in failed.content


def test_registry_caps_output_even_if_an_executor_exceeds_its_limit(tmp_path: Path) -> None:
    from app.application.tools import ToolRegistry

    class OversizedExecutor:
        async def execute(
            self, name: str, arguments: dict[str, object], workspace_root: Path
        ) -> tuple[str, bool]:
            return "API_KEY=hidden\n" + "a" * 25_000, False

    result = asyncio.run(
        ToolRegistry(OversizedExecutor()).execute(
            ModelToolCall(id="large-result", name="read_file", arguments={"path": "note.txt"}),
            tmp_path,
        )
    )

    assert not result.is_error
    assert result.truncated
    assert len(result.content.encode("utf-8")) <= 20_000
    assert "hidden" not in result.content
    assert "[REDACTED]" in result.content


def test_git_inspection_uses_fixed_operations_and_requires_workspace_repo_root(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("after\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()

    status = run_tool(tmp_path, "inspect_git", operation="status")
    diff = run_tool(tmp_path, "inspect_git", operation="diff")
    log = run_tool(tmp_path, "inspect_git", operation="log")
    nested = run_tool(tmp_path / "nested", "inspect_git", operation="status")
    arbitrary = run_tool(tmp_path, "inspect_git", operation="reset")

    assert "tracked.txt" in status.content
    assert "-before" in diff.content and "+after" in diff.content
    assert "Initial" in log.content
    assert nested.error_code == "invalid_workspace"
    assert arbitrary.error_code == "invalid_tool_arguments"


def test_git_status_and_diff_do_not_expose_protected_file_names_or_content(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".env").write_text("API_KEY=before\n", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("//registry/:_authToken=before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env", ".npmrc"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Initial",
        ],
        check=True,
    )
    (tmp_path / ".env").write_text("API_KEY=after\n", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("//registry/:_authToken=after\n", encoding="utf-8")

    status = run_tool(tmp_path, "inspect_git", operation="status")
    diff = run_tool(tmp_path, "inspect_git", operation="diff")

    assert ".env" not in status.content + diff.content
    assert ".npmrc" not in status.content + diff.content
    assert "API_KEY" not in status.content + diff.content


def test_git_inspection_uses_a_clean_environment_and_reports_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ -n "$OPENAI_API_KEY" ]; then exit 9; fi\n'
        f"case \"$*\" in *rev-parse*) printf '%s\\n' '{tmp_path}' ;; "
        "*) exit 7 ;; esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-git")

    result = run_tool(tmp_path, "inspect_git", operation="status")

    assert result.error_code == "git_failed"
    assert "must-not-reach-git" not in result.content


def test_git_inspection_reports_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))

    result = run_tool(tmp_path, "inspect_git", operation="status")

    assert result.error_code == "git_unavailable"


def test_git_inspection_kills_a_command_that_exceeds_its_time_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure import local_tools

    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *rev-parse*) printf '%s\\n' '{tmp_path}' ;; "
        "*) exec sleep 30 ;; esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(local_tools, "TOOL_TIMEOUT_SECONDS", 0.05)

    result = run_tool(tmp_path, "inspect_git", operation="status")

    assert result.error_code == "tool_timeout"


def test_git_diff_rejects_an_oversized_name_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *rev-parse*) printf '%s\\n' '{tmp_path}' ;; "
        "*) head -c 70000 /dev/zero | tr '\\0' 'a' ;; esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = run_tool(tmp_path, "inspect_git", operation="diff")

    assert result.error_code == "git_output_limit"


def test_git_inspection_suppresses_oversized_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *rev-parse*) printf '%s\\n' '{tmp_path}' ;; "
        "*) head -c 5000 /dev/zero | tr '\\0' 'S' >&2 ;; esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = run_tool(tmp_path, "inspect_git", operation="status")

    assert result.is_error
    assert result.error_code == "git_output_limit"
    assert "SSSS" not in result.content
