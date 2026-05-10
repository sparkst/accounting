"""Tests for svelte-check-on-edit hook.

The hook fires on Edit|Write, filters to dashboard/ TypeScript/Svelte files,
and runs svelte-check from the dashboard/ directory.

For deterministic, fast tests, the hook honors `SVELTE_CHECK_CMD` env var as an
override for the actual svelte-check invocation. Tests set it to a tagged echo
command and assert the marker appears in output. Production runs use the default
`npx --no-install svelte-check`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "svelte_check_on_edit.sh"
MARKER = "SVELTE_CHECK_CALLED"
STUB_CMD = f"echo {MARKER}"


@pytest.fixture(autouse=True)
def isolated_debounce(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh debounce file so they don't interfere."""
    f = tmp_path_factory.mktemp("debounce") / "last_run"
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_FILE", str(f))
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_SECONDS", "0")  # disable by default
    return f


def run_hook(tool_input: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps(tool_input)
    env["SVELTE_CHECK_CMD"] = STUB_CMD
    return subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )


def test_hook_script_exists_and_is_executable() -> None:
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK)


@pytest.mark.parametrize(
    "rel_path",
    [
        "dashboard/src/routes/+page.svelte",
        "dashboard/src/lib/api.ts",
        "dashboard/src/lib/types.ts",
        "dashboard/src/routes/+page.svelte.ts",
        "dashboard/svelte.config.js",
        "dashboard/vite.config.ts",
        "dashboard/tsconfig.json",
    ],
)
def test_dashboard_source_and_config_files_trigger_check(rel_path: str) -> None:
    """Source files and the dashboard config files all influence svelte-check output."""
    result = run_hook({"file_path": str(REPO_ROOT / rel_path)})
    assert result.returncode == 0
    assert MARKER in result.stdout


def test_dashboard_relative_path_also_triggers_check() -> None:
    """Bare `dashboard/...` paths (no leading slash) must also trigger."""
    result = run_hook({"file_path": "dashboard/src/lib/api.ts"})
    assert result.returncode == 0
    assert MARKER in result.stdout


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/api/main.py",
        "scripts/backup.sh",
        "dashboard/README.md",
        "CLAUDE.md",
        "dashboard/src/app.css",
        "dashboard/package.json",
        "dashboard/package-lock.json",
    ],
)
def test_non_dashboard_or_unsupported_extensions_skip(rel_path: str) -> None:
    """package.json and package-lock.json don't affect svelte-check output."""
    result = run_hook({"file_path": str(REPO_ROOT / rel_path)})
    assert result.returncode == 0
    assert MARKER not in result.stdout
    assert result.stderr == ""


def test_dashboard_node_modules_files_are_skipped() -> None:
    """Edits inside dashboard/node_modules/ shouldn't trigger a project check."""
    result = run_hook({"file_path": str(REPO_ROOT / "dashboard/node_modules/foo/index.ts")})
    assert result.returncode == 0
    assert MARKER not in result.stdout


def test_dashboard_svelte_kit_generated_files_are_skipped() -> None:
    """Edits inside dashboard/.svelte-kit/ are generated; skip."""
    result = run_hook({"file_path": str(REPO_ROOT / "dashboard/.svelte-kit/generated/x.ts")})
    assert result.returncode == 0
    assert MARKER not in result.stdout


def test_nested_tool_input_file_path_is_extracted() -> None:
    result = run_hook({"tool_input": {"file_path": "dashboard/src/lib/api.ts"}})
    assert result.returncode == 0
    assert MARKER in result.stdout


def test_blank_input_is_silent_noop() -> None:
    for tool_input in ({}, {"file_path": ""}, {"file_path": None}):
        result = run_hook(tool_input)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_unset_claude_tool_input_is_silent_noop() -> None:
    env = os.environ.copy()
    env.pop("CLAUDE_TOOL_INPUT", None)
    env["SVELTE_CHECK_CMD"] = STUB_CMD
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_hook_works_when_invoked_from_non_repo_cwd(tmp_path: Path) -> None:
    """Hook must derive paths via BASH_SOURCE even when cwd is elsewhere."""
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps({"file_path": "dashboard/src/lib/api.ts"})
    env["SVELTE_CHECK_CMD"] = STUB_CMD
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0
    assert MARKER in result.stdout


def test_hook_runs_check_from_dashboard_directory() -> None:
    """svelte-check needs to run from dashboard/ to find its config."""
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps({"file_path": "dashboard/src/lib/api.ts"})
    env["SVELTE_CHECK_CMD"] = "pwd"
    env["SVELTE_CHECK_DEBOUNCE_SECONDS"] = "0"
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    assert result.returncode == 0
    # The pwd output should be the dashboard dir
    assert "/dashboard" in result.stdout.strip()


def test_debounce_not_committed_on_failed_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the check command exits non-zero, the next invocation must still run."""
    debounce_file = tmp_path / "last_run"
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_FILE", str(debounce_file))
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_SECONDS", "60")
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps({"file_path": "dashboard/src/lib/api.ts"})
    env["SVELTE_CHECK_CMD"] = "false"  # always fails
    env["SVELTE_CHECK_DEBOUNCE_FILE"] = str(debounce_file)
    env["SVELTE_CHECK_DEBOUNCE_SECONDS"] = "60"
    first = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
    assert first.returncode == 0  # hook itself always exits 0
    # debounce file should NOT have been written, so a second run still triggers
    assert not debounce_file.exists() or debounce_file.read_text().strip() in ("", "0")


def test_debounce_skips_back_to_back_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second invocation within the debounce window must not run svelte-check."""
    debounce_file = tmp_path / "last_run"
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_FILE", str(debounce_file))
    monkeypatch.setenv("SVELTE_CHECK_DEBOUNCE_SECONDS", "60")
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps({"file_path": "dashboard/src/lib/api.ts"})
    env["SVELTE_CHECK_CMD"] = STUB_CMD
    env["SVELTE_CHECK_DEBOUNCE_FILE"] = str(debounce_file)
    env["SVELTE_CHECK_DEBOUNCE_SECONDS"] = "60"
    # First call runs
    first = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
    assert first.returncode == 0
    assert MARKER in first.stdout
    # Second call within window is debounced
    second = subprocess.run(["bash", str(HOOK)], env=env, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
    assert second.returncode == 0
    assert MARKER not in second.stdout
