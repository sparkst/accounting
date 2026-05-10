"""Tests for alembic-migration invariant checker."""
from __future__ import annotations

from pathlib import Path

import pytest
from check_migration import Severity, check_file, format_findings


def write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


SAFE_MIGRATION = '''"""add a benign column"""
from alembic import op
import sqlalchemy as sa

revision = "abc123"
down_revision = "def456"


def upgrade() -> None:
    """Add an optional notes column."""
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop the notes column."""
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("notes")
'''


def test_safe_migration_produces_no_findings(tmp_path: Path) -> None:
    f = tmp_path / "safe.py"
    write(f, SAFE_MIGRATION)
    findings = check_file(f)
    assert findings == []


def test_dropping_raw_data_is_flagged_p0(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.drop_column("notes")',
            'batch_op.drop_column("raw_data")',
        ).replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'batch_op.drop_column("raw_data")',
        ),
    )
    findings = check_file(f)
    assert any(x.severity == Severity.P0 and "raw_data" in x.message for x in findings)


def test_dropping_audit_columns_is_flagged_p0(tmp_path: Path) -> None:
    for col in ("created_at", "updated_at", "confirmed_by"):
        f = tmp_path / f"drop_{col}.py"
        write(
            f,
            SAFE_MIGRATION.replace(
                'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
                f'batch_op.drop_column("{col}")',
            ),
        )
        findings = check_file(f)
        assert any(x.severity == Severity.P0 and col in x.message for x in findings), col


def test_delete_from_transactions_is_flagged_p0(tmp_path: Path) -> None:
    f = tmp_path / "delete.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.execute("DELETE FROM transactions WHERE id < 100")',
        ),
    )
    findings = check_file(f)
    assert any(x.severity == Severity.P0 and "DELETE" in x.message.upper() for x in findings)


def test_drop_transactions_table_is_flagged_p0(tmp_path: Path) -> None:
    f = tmp_path / "drop_table.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'with op.batch_alter_table("transactions") as batch_op:\n        batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.drop_table("transactions")',
        ),
    )
    findings = check_file(f)
    assert findings  # don't allow an empty-list vacuous truth
    assert any(
        x.severity == Severity.P0 and "transactions" in x.message and "drop" in x.message.lower()
        for x in findings
    )


def test_drop_audit_event_table_is_flagged_p0(tmp_path: Path) -> None:
    """audit_event is also a protected table — dropping it breaks the audit trail."""
    f = tmp_path / "drop_audit.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'with op.batch_alter_table("transactions") as batch_op:\n        batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.drop_table("audit_event")',
        ),
    )
    findings = check_file(f)
    assert findings
    assert any(
        x.severity == Severity.P0 and "audit_event" in x.message for x in findings
    )


def test_delete_via_sa_text_wrapper_is_flagged_p0(tmp_path: Path) -> None:
    """op.execute(sa.text("DELETE FROM ...")) must be caught — common idiom."""
    f = tmp_path / "delete_text.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.execute(sa.text("DELETE FROM transactions WHERE id < 100"))',
        ),
    )
    findings = check_file(f)
    assert findings
    assert any(x.severity == Severity.P0 and "DELETE" in x.message.upper() for x in findings)


def test_delete_via_text_bindparams_chain_is_flagged_p0(tmp_path: Path) -> None:
    """op.execute(sa.text("DELETE...").bindparams(...)) must still be caught."""
    f = tmp_path / "delete_chain.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.execute(sa.text("DELETE FROM transactions").bindparams())',
        ),
    )
    findings = check_file(f)
    assert findings
    assert any(x.severity == Severity.P0 and "DELETE" in x.message.upper() for x in findings)


def test_delete_via_text_keyword_arg_is_flagged_p0(tmp_path: Path) -> None:
    """op.execute(statement=sa.text("DELETE...")) must be caught."""
    f = tmp_path / "delete_kw.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'op.execute(statement=sa.text("DELETE FROM transactions"))',
        ),
    )
    findings = check_file(f)
    assert findings
    assert any(x.severity == Severity.P0 and "DELETE" in x.message.upper() for x in findings)


def test_empty_downgrade_is_flagged_p1(tmp_path: Path) -> None:
    body = SAFE_MIGRATION.replace(
        '''def downgrade() -> None:
    """Drop the notes column."""
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("notes")''',
        '''def downgrade() -> None:
    pass''',
    )
    f = tmp_path / "no_down.py"
    write(f, body)
    findings = check_file(f)
    assert any(x.severity == Severity.P1 and "downgrade" in x.message.lower() for x in findings)


def test_missing_downgrade_function_is_flagged_p1(tmp_path: Path) -> None:
    body = SAFE_MIGRATION.split("def downgrade")[0]
    f = tmp_path / "no_down.py"
    write(f, body)
    findings = check_file(f)
    assert findings
    assert any(x.severity == Severity.P1 and "downgrade" in x.message.lower() for x in findings)


def test_merge_migration_with_empty_downgrade_is_not_flagged(tmp_path: Path) -> None:
    """A migration whose down_revision is a tuple is a merge — empty downgrade is correct."""
    f = tmp_path / "merge.py"
    body = SAFE_MIGRATION.replace(
        'down_revision = "def456"',
        'down_revision: tuple[str, ...] = ("rev1", "rev2")',
    ).replace(
        '''def downgrade() -> None:
    """Drop the notes column."""
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("notes")''',
        '''def downgrade() -> None:
    pass''',
    )
    write(f, body)
    findings = check_file(f)
    # Merge migration's `pass` downgrade is legitimate
    assert not any(
        x.severity == Severity.P1 and "downgrade" in x.message.lower() for x in findings
    )


def test_findings_include_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.add_column(sa.Column("notes", sa.String(), nullable=True))',
            'batch_op.drop_column("raw_data")',
        ),
    )
    findings = check_file(f)
    p0 = next(x for x in findings if x.severity == Severity.P0)
    assert p0.line > 0


def test_format_findings_returns_human_readable_text(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    write(
        f,
        SAFE_MIGRATION.replace(
            'batch_op.drop_column("notes")',
            'batch_op.drop_column("raw_data")',
        ),
    )
    findings = check_file(f)
    out = format_findings(f, findings)
    assert str(f) in out
    assert "P0" in out


def test_format_findings_handles_empty_list(tmp_path: Path) -> None:
    f = tmp_path / "ok.py"
    write(f, SAFE_MIGRATION)
    out = format_findings(f, [])
    assert "no issues" in out.lower() or "ok" in out.lower()


@pytest.mark.parametrize("col", ["raw_data", "created_at", "updated_at", "confirmed_by"])
def test_drop_column_via_quoted_string_only(tmp_path: Path, col: str) -> None:
    """The column name must be in a string literal — bare references should not match.

    e.g. an import `from x import created_at` should not be flagged.
    """
    body = SAFE_MIGRATION.replace(
        'from alembic import op\nimport sqlalchemy as sa',
        f'from alembic import op\nimport sqlalchemy as sa\nfrom mymod import {col}  # noqa\n',
    )
    f = tmp_path / "import.py"
    write(f, body)
    findings = check_file(f)
    # Bare name reference must not raise a P0 drop_column finding for that col
    assert not any(
        x.severity == Severity.P0 and "drop" in x.message.lower() and col in x.message
        for x in findings
    )
