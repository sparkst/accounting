"""Tests for mypy-on-edit hook script.

The hook is invoked by Claude Code's PostToolUse handler with the tool's input
JSON in $CLAUDE_TOOL_INPUT. The hook extracts the edited file path, decides
whether mypy should run on it, and executes mypy if so. Hook output goes to
stdout/stderr where Claude can read it.

Behavior contract:
- File must end in `.py`. Other extensions: silent no-op, exit 0.
- File must be under `src/` (or anywhere outside `dashboard/`). dashboard/ is
  excluded because it's a SvelteKit project, not a Python package.
- File must exist on disk. Missing file: silent no-op, exit 0.
- mypy errors are written to stdout so Claude sees them.
- Hook always exits 0 — type errors are surfaced, not blocking. (Blocking
  would be PreToolUse; this is PostToolUse and runs after the edit.)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "mypy_on_edit.sh"


def run_hook(tool_input: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps(tool_input)
    return subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def test_hook_script_exists_and_is_executable() -> None:
    assert HOOK.is_file(), f"hook script not found at {HOOK}"
    assert os.access(HOOK, os.X_OK), f"hook script not executable: {HOOK}"


def test_clean_python_file_produces_no_errors(tmp_path: Path) -> None:
    f = tmp_path / "clean.py"
    f.write_text("x: int = 1\n")
    result = run_hook({"file_path": str(f)})
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "error:" not in combined.lower()


def test_python_file_with_type_error_surfaces_mypy_output(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text("x: int = 'a string'\n")
    result = run_hook({"file_path": str(f)})
    # Hook should still exit 0 — surface the error, don't block
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "error:" in combined.lower()
    assert "incompatible" in combined.lower() or "int" in combined.lower()


def test_non_python_file_is_skipped(tmp_path: Path) -> None:
    f = tmp_path / "thing.txt"
    f.write_text("not python\n")
    result = run_hook({"file_path": str(f)})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_dashboard_python_files_are_skipped() -> None:
    """A .py file under dashboard/ (if any) should not trigger mypy — absolute path."""
    dashboard_path = REPO_ROOT / "dashboard" / "fake.py"
    result = run_hook({"file_path": str(dashboard_path)})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_dashboard_python_files_are_skipped_with_relative_path() -> None:
    """Relative dashboard/foo.py paths must also skip — absolute prefix is not always present."""
    result = run_hook({"file_path": "dashboard/src/lib/foo.py"})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_nested_tool_input_file_path_is_extracted(tmp_path: Path) -> None:
    """Some harness shapes wrap fields in `tool_input` — fallback path must work."""
    f = tmp_path / "bad.py"
    f.write_text("x: int = 'wrong'\n")
    result = run_hook({"tool_input": {"file_path": str(f)}})
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "error:" in combined.lower()


def test_missing_file_is_silent_noop(tmp_path: Path) -> None:
    f = tmp_path / "missing.py"
    result = run_hook({"file_path": str(f)})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_empty_tool_input_is_silent_noop() -> None:
    """A tool input with no file_path key should be a no-op."""
    result = run_hook({})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_tool_input_with_quoted_special_chars_does_not_break(tmp_path: Path) -> None:
    """File path with spaces and quotes should be handled safely."""
    f = tmp_path / 'has space.py'
    f.write_text("x: int = 1\n")
    result = run_hook({"file_path": str(f)})
    assert result.returncode == 0


def test_files_outside_repo_are_still_type_checked(tmp_path: Path) -> None:
    """A .py file outside the repo (e.g. /tmp) should still be type-checked.

    The dashboard exclusion is the only path-based skip. Any other .py is fair game.
    """
    f = tmp_path / "outside.py"
    f.write_text("x: int = 'wrong'\n")
    result = run_hook({"file_path": str(f)})
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "error:" in combined.lower()


def test_hook_works_when_invoked_from_non_repo_cwd(tmp_path: Path) -> None:
    """REPO_ROOT must resolve via BASH_SOURCE even when bash is run from elsewhere."""
    f = tmp_path / "bad.py"
    f.write_text("x: int = 'wrong'\n")
    env = os.environ.copy()
    env["CLAUDE_TOOL_INPUT"] = json.dumps({"file_path": str(f)})
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),  # not REPO_ROOT
        timeout=60,
    )
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    assert "error:" in combined.lower()


def test_unset_claude_tool_input_is_silent_noop(tmp_path: Path) -> None:
    """Hook must not crash under set -u when CLAUDE_TOOL_INPUT is absent from env."""
    env = os.environ.copy()
    env.pop("CLAUDE_TOOL_INPUT", None)
    result = subprocess.run(
        ["bash", str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "tool_input",
    [
        {"file_path": ""},
        {"file_path": None},
    ],
)
def test_blank_file_path_is_silent_noop(tool_input: dict[str, object]) -> None:
    result = run_hook(tool_input)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
