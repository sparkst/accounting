"""Repair vendor rules whose regex pattern is stored as a literal (REQ-FIX-ING-023).

Incident 2026-09-02. REQ-FIX-ING-005 defined ``is_regex=False`` as
``re.escape``-ed literal matching, and the migration that added the flag
defaulted every pre-existing row to False. The human-authored regex rules
(``cardinal.*health|fascinate.*os``, ``\\bshopify\\b``, ``amazon.*aws|aws\\.amazon``)
became literals that can never match a bank description, so 34 of 76 production
rules went silently dead and Tier 1 stopped answering for half the vendor book.
Two $31,000 Cardinal Health consulting payments then fell through to Tier 3 and
were classified as personal MEDICAL expenses.

``src/classification/rules.py`` now refuses to store that combination. This
script repairs the rows already carrying it.

A pattern that carries regex constructs but does NOT compile is left alone and
reported — flipping it would trade a silent miss for a logged one, and a human
has to decide what it was meant to say.

Usage:
    python -m scripts.repair_vendor_rule_patterns              # dry-run
    python -m scripts.repair_vendor_rule_patterns --apply      # write
    python -m scripts.repair_vendor_rule_patterns --json       # machine-readable
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from src.classification.rules import (  # noqa: E402
    PatternRepairResult,
    repair_literal_regex_rules,
)
from src.db.connection import SessionLocal  # noqa: E402
from src.models.vendor_rule import VendorRule  # noqa: E402

logger = logging.getLogger(__name__)


def repair(
    session: Session,
    *,
    dry_run: bool = True,
    changed_by: str = "human:operator",
) -> PatternRepairResult:
    """Run the repair and own the commit. DRY-RUN default (REQ-FIX-ING-023).

    ``changed_by`` is stamped on the AuditEvent for each flipped rule. This is
    a manually-run CLI (no cron/timer references it), so the default names a
    human operator; pass ``--changed-by`` to record who actually ran it.
    """
    result = repair_literal_regex_rules(
        session, dry_run=dry_run, changed_by=changed_by
    )
    if not dry_run and result.repaired:
        session.commit()
    return result


def _rule_labels(session: Session, ids: list[str]) -> list[dict[str, object]]:
    """Per-rule detail for the operator: what changed, and how much it carried."""
    if not ids:
        return []
    rows = session.query(VendorRule).filter(VendorRule.id.in_(ids)).all()
    return [
        {
            "id": r.id,
            "pattern": r.vendor_pattern,
            "entity": r.entity,
            "tax_category": r.tax_category,
            "direction": r.direction,
            "source": r.source,
            "confidence": r.confidence,
            "examples": r.examples,
            "last_matched": str(r.last_matched) if r.last_matched else None,
        }
        for r in rows
    ]


def _print_summary(
    result: PatternRepairResult,
    repaired: list[dict[str, object]],
    skipped: list[dict[str, object]],
) -> None:
    mode = "DRY-RUN (no writes)" if result.dry_run else "APPLIED"
    print(f"\nVendor-rule pattern repair — {mode}")
    print(f"  repaired: {result.repaired}")
    print(f"  skipped (pattern does not compile): {result.skipped}")

    if repaired:
        print("\n  Rules switched to is_regex=True:")
        for r in repaired:
            print(
                f"    {r['pattern']!r}\n"
                f"      entity={r['entity']} category={r['tax_category']} "
                f"direction={r['direction']} source={r['source']} "
                f"conf={r['confidence']} examples={r['examples']} "
                f"last_matched={r['last_matched']}"
            )
    if skipped:
        print("\n  Left for a human (neither a valid literal nor a valid regex):")
        for r in skipped:
            print(f"    {r['pattern']!r}  (id={r['id']})")
    if result.dry_run and result.repaired:
        print("\n  Re-run with --apply to write.")
    print()


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit (default: dry-run).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p.add_argument(
        "--changed-by",
        default="human:operator",
        help="Actor recorded on each rule's AuditEvent (default: human:operator).",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    session = SessionLocal()
    try:
        # Capture the before-state while the rows are still unrepaired, so the
        # report shows what each rule was carrying when it was dead.
        preview = repair_literal_regex_rules(session, dry_run=True)
        repaired_detail = _rule_labels(session, preview.repaired_ids)
        skipped_detail = _rule_labels(session, preview.skipped_ids)

        result = repair(session, dry_run=not args.apply, changed_by=args.changed_by)

        if args.json:
            print(
                json.dumps(
                    {
                        "dry_run": result.dry_run,
                        "repaired": result.repaired,
                        "skipped": result.skipped,
                        "repaired_rules": repaired_detail,
                        "skipped_rules": skipped_detail,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            _print_summary(result, repaired_detail, skipped_detail)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
