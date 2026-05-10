#!/usr/bin/env bash
# PostToolUse hook: run svelte-check when Claude edits a dashboard TS/Svelte file.
# Always exits 0 — surfaces errors but does not block.
#
# svelte-check runs against the whole project (no per-file mode), so this hook
# is somewhat heavy. To keep latency reasonable, set SVELTE_CHECK_CMD in the env
# to override the default invocation (used by tests with a fast stub).
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DASHBOARD_DIR="${REPO_ROOT}/dashboard"
SVELTE_CHECK_CMD="${SVELTE_CHECK_CMD:-npx --no-install svelte-check --threshold error}"
# Debounce: skip if a full project check ran within the last N seconds. svelte-check
# always runs against the whole project (no per-file mode), so back-to-back edits in
# the same task would otherwise queue 5-10 redundant 5-10s checks.
DEBOUNCE_SECONDS="${SVELTE_CHECK_DEBOUNCE_SECONDS:-15}"
DEBOUNCE_FILE="${SVELTE_CHECK_DEBOUNCE_FILE:-/tmp/svelte_check_last_run}"

# Extract file_path from $CLAUDE_TOOL_INPUT (JSON) — tries top-level then nested.
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

# Match supported extensions: .svelte, .ts (covers .svelte.ts SvelteKit route scripts
# automatically), .js (svelte.config.js, vite.config.js). For .json, only
# tsconfig*.json / jsconfig.json — package.json and package-lock.json don't influence
# svelte-check output and shouldn't burn a debounce window.
case "$file" in
    *.svelte|*.ts|*.js) ;;
    */tsconfig*.json|*/jsconfig.json|tsconfig*.json|jsconfig.json) ;;
    *) exit 0 ;;
esac

# File must be inside dashboard/ (absolute or relative path) but not under
# dashboard/node_modules/ or dashboard/.svelte-kit/.
case "$file" in
    *"/dashboard/node_modules/"*|"dashboard/node_modules/"*) exit 0 ;;
    *"/dashboard/.svelte-kit/"*|"dashboard/.svelte-kit/"*) exit 0 ;;
esac

case "$file" in
    *"/dashboard/"*|"dashboard/"*) ;;
    *) exit 0 ;;
esac

# Dashboard directory must exist
if [ ! -d "$DASHBOARD_DIR" ]; then
    exit 0
fi

# Debounce: if a check ran recently, skip this one.
now=$(date +%s)
if [ -f "$DEBOUNCE_FILE" ]; then
    last=$(cat "$DEBOUNCE_FILE" 2>/dev/null || echo 0)
    if [ -n "$last" ] && [ "$last" -gt 0 ] && [ $((now - last)) -lt "$DEBOUNCE_SECONDS" ]; then
        exit 0
    fi
fi

# Run from the dashboard directory so svelte-check finds its config and tsconfig.
cd "$DASHBOARD_DIR" || exit 0
# shellcheck disable=SC2086  # SVELTE_CHECK_CMD is intentionally word-split for tests
$SVELTE_CHECK_CMD 2>&1
rc=$?
# Only commit the debounce timestamp on success, so a crashed/killed check
# doesn't silence the next invocation.
if [ "$rc" -eq 0 ]; then
    echo "$now" > "$DEBOUNCE_FILE" 2>/dev/null || true
fi
exit 0
