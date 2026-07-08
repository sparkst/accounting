"""vendor_rules gains is_regex

Revision ID: vr_isregex01_vendor_rule_is_regex
Revises: alr01_alert_dispatch_payload
Create Date: 2026-07-07 00:00:02.000000

REQ-FIX-ING-005: vendor_pattern was compiled as a raw regex, so learned
patterns (verbatim vendor descriptions) let metacharacters (``.`` ``$`` ``(``
``+``) silently over-match — e.g. "A.B Corp" matched "A9B Corporate". Adding
``is_regex`` makes the matcher flag-driven: patterns match as a literal
(case-insensitive substring, escaped at match time) unless explicitly flagged
regex. This is a schema-only migration — no data rewrite. Every existing row
gets ``is_regex=false`` via the server default, flipping from raw-regex to
literal matching, which is the desired behavior change (see design spec
§2.5). ``_upsert_vendor_rule`` always writes ``is_regex=False`` for learned
rules; write paths accepting ``is_regex=true`` must validate via
``re.compile()`` at write time.

Downgrade drops the column — a real reversal. Literal patterns revert to
raw-regex matching (pre-fix behavior); acceptable and documented. No touch of
transactions/audit_event/raw_data/timestamps; no DELETEs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "vr_isregex01_vendor_rule_is_regex"
down_revision: str | None = "alr01_alert_dispatch_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vendor_rules") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_regex",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("vendor_rules") as batch_op:
        batch_op.drop_column("is_regex")
