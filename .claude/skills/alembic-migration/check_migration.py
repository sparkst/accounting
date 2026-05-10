"""Static checker for Alembic migrations against this project's invariants.

Run before committing any new Alembic migration:

    python3 .claude/skills/alembic-migration/check_migration.py path/to/migration.py [...]

Exits 0 with a clean report when no findings; exits 1 when any P0/P1 finding fires.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    line: int
    message: str


# Columns whose disappearance violates the audit-trail rule.
PROTECTED_TX_COLUMNS = frozenset({"raw_data", "created_at", "updated_at", "confirmed_by"})

# Tables that must never be dropped or wholesale-deleted from.
PROTECTED_TABLES = frozenset({"transactions", "audit_event"})


def _strings_in_call(call: ast.Call) -> list[tuple[str, int]]:
    """Return (string_value, lineno) for every string literal arg/keyword in a call."""
    out: list[tuple[str, int]] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append((arg.value, getattr(arg, "lineno", call.lineno)))
    for kw in call.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            out.append((kw.value.value, getattr(kw.value, "lineno", call.lineno)))
    return out


def _attr_chain(node: ast.expr) -> str:
    """Render an attribute access chain as 'a.b.c' or '' if not an attribute."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _called_name(call: ast.Call) -> str:
    """The dotted name of a call's callee, e.g. 'op.drop_table' or 'batch_op.drop_column'."""
    if isinstance(call.func, ast.Attribute):
        return _attr_chain(call.func)
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _walk_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def check_file(path: Path) -> list[Finding]:
    """Return invariant findings for an Alembic migration file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return [Finding(Severity.P0, e.lineno or 1, f"file does not parse: {e.msg}")]

    findings.extend(_check_protected_columns(tree))
    findings.extend(_check_destructive_ops(tree, text))
    findings.extend(_check_downgrade(tree))

    findings.sort(key=lambda f: (f.severity.value, f.line))
    return findings


def _check_protected_columns(tree: ast.AST) -> list[Finding]:
    """Flag drop_column / alter_column drops that target protected audit fields."""
    findings: list[Finding] = []
    for call in _walk_calls(tree):
        name = _called_name(call)
        if not name.endswith(".drop_column") and name != "drop_column":
            continue
        for value, lineno in _strings_in_call(call):
            if value in PROTECTED_TX_COLUMNS:
                findings.append(
                    Finding(
                        Severity.P0,
                        lineno,
                        f"drops protected audit column {value!r} — this column is required by the audit-trail rule",
                    )
                )
    return findings


def _check_destructive_ops(tree: ast.AST, text: str) -> list[Finding]:
    """Flag drop_table on protected tables and any DELETE FROM in op.execute()."""
    findings: list[Finding] = []
    for call in _walk_calls(tree):
        name = _called_name(call)
        if name.endswith("drop_table"):
            for value, lineno in _strings_in_call(call):
                if value in PROTECTED_TABLES:
                    findings.append(
                        Finding(
                            Severity.P0,
                            lineno,
                            f"drops protected audit table {value!r} — audit-trail tables must never be dropped",
                        )
                    )
        elif name.endswith("execute"):
            # Plain string: op.execute("DELETE FROM ...")
            for value, lineno in _strings_in_call(call):
                if _has_delete(value):
                    findings.append(
                        Finding(
                            Severity.P0,
                            lineno,
                            "raw DELETE in op.execute() — never delete audit-trail data",
                        )
                    )
            # Wrapped: op.execute(sa.text("DELETE FROM ...")), op.execute(text(...).bindparams(...)),
            # op.execute(statement=text(...)). Walk every descendant Call to find any text(...).
            arg_nodes: list[ast.expr] = list(call.args) + [kw.value for kw in call.keywords]
            for top in arg_nodes:
                for sub in ast.walk(top):
                    if not isinstance(sub, ast.Call):
                        continue
                    callee = _called_name(sub)
                    if callee.endswith(".text") or callee == "text":
                        for value, lineno in _strings_in_call(sub):
                            if _has_delete(value):
                                findings.append(
                                    Finding(
                                        Severity.P0,
                                        lineno,
                                        "raw DELETE in op.execute(text(...)) — never delete audit-trail data",
                                    )
                                )
    return findings


def _has_delete(sql: str) -> bool:
    return bool(re.search(r"\bDELETE\s+FROM\b", sql, re.IGNORECASE))


def _check_downgrade(tree: ast.AST) -> list[Finding]:
    """Flag missing or empty downgrade(), unless this is a merge migration.

    A merge migration has a tuple/list `down_revision` and legitimately has an
    empty downgrade — there's nothing to undo.
    """
    if _is_merge_migration(tree):
        return []
    findings: list[Finding] = []
    downgrade: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            downgrade = node
            break
    if downgrade is None:
        findings.append(
            Finding(Severity.P1, 1, "missing downgrade() function — every migration needs a reverse path")
        )
        return findings
    body = [stmt for stmt in downgrade.body if not _is_docstring(stmt)]
    if not body or all(isinstance(stmt, ast.Pass) for stmt in body):
        findings.append(
            Finding(
                Severity.P1,
                downgrade.lineno,
                "downgrade() is empty — provide a real reverse, even if it raises NotImplementedError with a reason",
            )
        )
    return findings


def _is_merge_migration(tree: ast.AST) -> bool:
    """A migration whose `down_revision` is a tuple or list — a merge of multiple heads."""
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "down_revision":
                return isinstance(node.value, (ast.Tuple, ast.List))
        # Annotated assignment: `down_revision: ... = (...)`
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "down_revision"
            and isinstance(node.value, (ast.Tuple, ast.List))
        ):
            return True
    return False


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def format_findings(path: Path, findings: list[Finding]) -> str:
    if not findings:
        return f"{path}: ok — no issues found"
    lines = [f"{path}:"]
    for f in findings:
        lines.append(f"  [{f.severity.value}] line {f.line}: {f.message}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="check_migration",
        description="Check an Alembic migration against this project's audit-trail invariants.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="Migration files to check")
    args = parser.parse_args(argv[1:])
    rc = 0
    for p in args.paths:
        if not p.is_file():
            print(f"{p}: not found", file=sys.stderr)
            rc = 2
            continue
        findings = check_file(p)
        print(format_findings(p, findings))
        if findings:
            rc = max(rc, 1)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
