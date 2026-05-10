---
name: req-trace
description: Map REQ-IDs declared in requirements/current.md to the test files and impl files under src/ that reference them. Use when the user asks for REQ coverage, untested requirements, orphan tests, a coverage matrix, or wonders whether every REQ has tests — phrases like "what REQs aren't tested", "do we have tests for every REQ", "show me REQ coverage", "any orphan REQs", or "trace coverage for REQ-005c". Only scans .py files under src/; frontend, scripts, and migrations are out of scope. Runs from the repo root.
user_invocable: true
---

# REQ Trace

Build a coverage matrix from `requirements/current.md` REQ-IDs to the test files and impl files that mention them.

## When to invoke

- Before adding a new requirement, to confirm existing ones are covered.
- During a TDD red phase, to confirm a new REQ is mentioned in a failing test before implementation begins.
- When auditing coverage end-to-end (e.g., before a release).

## How to invoke

Always cd to the repo root first — the script's defaults are relative paths.

```bash
cd /Users/travis/SGDrive/dev/accounting && python3 .claude/skills/req-trace/req_trace.py
```

`python3` (system) is fine — the script uses only the standard library, so no venv activation is required.

Optional flags: `--requirements PATH` and `--src PATH`. Defaults are `requirements/current.md` and `src/`.

## What to do with the output

Print the full matrix output inline in the conversation. Then call out any non-`covered` rows in a one-line summary so the user can act on them. The final `## Summary` block in the output gives the tally.

## Output states

| State | Meaning |
|---|---|
| `covered` | At least one test file and one impl file reference the REQ |
| `no tests` | Implemented but not referenced by any test |
| `no impl` | Test references a REQ that no impl file mentions |
| `bare` | Declared in `requirements/current.md` but referenced nowhere else |
| `parent (see sub-REQs)` | A REQ-XXX heading whose body is split into REQ-XXXa/b/...; coverage lives on the sub-reqs |

## What it considers a test vs impl

A `.py` file under `--src` whose **filename starts with `test_`** is a test. Everything else under `--src` is impl. Files outside `--src` (top-level `scripts/`, `dashboard/`, alembic migrations) are not scanned — by design, since this codebase ties tests to REQ-IDs inside `src/`.

## Caveats

- The matcher is text-based. A REQ-ID inside a comment or docstring counts; that is intentional.
- Sub-requirements (`REQ-005a`, `REQ-005b`) are tracked independently from their parent (`REQ-005`). When a parent heading exists alongside sub-headings, the parent is rendered as `parent (see sub-REQs)` rather than reported as bare.
- File lists are sorted so the output is stable across filesystems and easy to diff.
