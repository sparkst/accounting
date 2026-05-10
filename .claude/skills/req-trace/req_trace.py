"""Build a coverage matrix from REQ-IDs to tests and implementation files.

Usage:
    python3 req_trace.py [--requirements PATH] [--src PATH]

Defaults: requirements/current.md and src/ relative to the current directory.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQ_PATTERN = re.compile(r"\bREQ-\d+[a-z]?\b")
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(REQ-\d+[a-z]?)\b", re.MULTILINE)


def declared_reqs(requirements_path: Path) -> list[str]:
    """REQ-IDs declared as headings in the requirements doc, in source order."""
    text = requirements_path.read_text(encoding="utf-8", errors="replace")
    seen: dict[str, None] = {}
    for match in HEADING_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def parent_id(req: str) -> str:
    """Return the parent REQ for a sub-req like REQ-005a → REQ-005, else the input."""
    return req[:-1] if req[-1].isalpha() else req


def build_matrix(requirements_path: Path, src_root: Path) -> dict[str, dict[str, list[str]]]:
    """Map each declared REQ-ID to the test files and impl files that mention it.

    A file is a *test* iff its filename starts with ``test_``. All other ``.py``
    files under ``src_root`` count as *impl*. File lists are returned sorted so
    the output is stable across filesystems.
    """
    reqs = declared_reqs(requirements_path)
    req_set = set(reqs)
    matrix: dict[str, dict[str, list[str]]] = {
        rid: {"tests": [], "impl": []} for rid in reqs
    }
    for path in src_root.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        mentioned = {m.group(0) for m in REQ_PATTERN.finditer(text)}
        bucket = "tests" if path.name.startswith("test_") else "impl"
        rel = str(path)
        for rid in mentioned & req_set:
            matrix[rid][bucket].append(rel)
    for entry in matrix.values():
        entry["tests"].sort()
        entry["impl"].sort()
    return matrix


def has_subreqs(rid: str, all_reqs: list[str]) -> bool:
    """True if rid is a parent (e.g. REQ-005) with declared sub-reqs (REQ-005a)."""
    return any(other != rid and parent_id(other) == rid for other in all_reqs)


def classify(entry: dict[str, list[str]]) -> str:
    if entry["tests"] and entry["impl"]:
        return "covered"
    if entry["impl"]:
        return "no tests"
    if entry["tests"]:
        return "no impl"
    return "bare"


PARENT_STATE = "parent (see sub-REQs)"


def format_matrix(matrix: dict[str, dict[str, list[str]]]) -> str:
    all_reqs = list(matrix.keys())
    lines = ["# REQ Coverage Matrix", ""]
    counts: dict[str, int] = {}
    for rid, entry in matrix.items():
        # A parent heading whose body is split into sub-reqs is always labeled
        # as a parent — even if the parent ID is also mentioned directly.
        # Direct mentions are still shown for traceability.
        state = PARENT_STATE if has_subreqs(rid, all_reqs) else classify(entry)
        counts[state] = counts.get(state, 0) + 1
        lines.append(f"## {rid} — {state}")
        if entry["tests"] or entry["impl"]:
            lines.append("- tests:")
            lines.extend(_render_files(entry["tests"]))
            lines.append("- impl:")
            lines.extend(_render_files(entry["impl"]))
        lines.append("")
    lines.append("## Summary")
    for state, n in sorted(counts.items()):
        lines.append(f"- {state}: {n}")
    return "\n".join(lines)


def _render_files(files: list[str]) -> list[str]:
    if not files:
        return ["  - (none)"]
    return [f"  - `{f}`" for f in files]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="req_trace",
        description="Map declared REQ-IDs to the test and impl files that mention them.",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path("requirements/current.md"),
        help="Path to the requirements markdown (default: requirements/current.md)",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=Path("src"),
        help="Source root to scan (default: src)",
    )
    args = parser.parse_args(argv[1:])
    if not args.requirements.is_file():
        print(f"requirements file not found: {args.requirements}", file=sys.stderr)
        return 2
    if not args.src.is_dir():
        print(f"src root not found: {args.src}", file=sys.stderr)
        return 2
    print(format_matrix(build_matrix(args.requirements, args.src)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
