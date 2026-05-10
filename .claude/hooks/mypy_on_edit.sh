#!/usr/bin/env bash
# PostToolUse hook: run mypy on Python files Claude just edited.
# Skips dashboard/ (SvelteKit), non-Python files, missing files, and blank inputs.
# Always exits 0 — surfaces type errors but does not block. PostToolUse can't
# undo the edit; blocking belongs in PreToolUse.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MYPY="${REPO_ROOT}/.venv/bin/mypy"
CONFIG="${REPO_ROOT}/pyproject.toml"
CACHE="${REPO_ROOT}/.mypy_cache"

# Extract file_path from $CLAUDE_TOOL_INPUT (JSON). Use python for robust parsing.
# Tries top-level `file_path` first, then nested `tool_input.file_path` to cover
# either harness wrapping shape.
file=$(python3 -c '
import json, os, sys
raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
if not raw:
    sys.exit(0)
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
fp = data.get("file_path")
if not isinstance(fp, str):
    nested = data.get("tool_input")
    if isinstance(nested, dict):
        fp = nested.get("file_path")
if isinstance(fp, str):
    print(fp)
' 2>/dev/null)

# No file path → silent no-op
if [ -z "$file" ]; then
    exit 0
fi

# Only Python files
case "$file" in
    *.py) ;;
    *) exit 0 ;;
esac

# Skip dashboard/ (TypeScript/Svelte project) — match both absolute and relative paths
case "$file" in
    *"/dashboard/"* | "dashboard/"*) exit 0 ;;
esac

# File must exist
if [ ! -f "$file" ]; then
    exit 0
fi

# mypy must be available
if [ ! -x "$MYPY" ]; then
    exit 0
fi

# Run mypy with the project config (so strict mode applies regardless of cwd)
# and a stable cache dir. Errors go to stdout for Claude. Always exit 0.
"$MYPY" --no-error-summary --config-file "$CONFIG" --cache-dir "$CACHE" "$file" 2>&1 || true
exit 0
