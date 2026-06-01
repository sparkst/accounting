# Plaid Transactions Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-ingest Chase (and any Plaid-linked depository) transactions into the cash-basis register via Plaid `/transactions/sync`, with Plaid as the sole source per linked account.

**Architecture:** A new `plaid_transactions.py` adapter mirrors the Phase-1 `plaid_balance.py` shape (DRY-RUN default, `sync_one_item`/`sync_all_active`, three layers of error isolation). Cursor-based incremental sync handles `added`/`modified`/`removed`; pending→posted reconcile keys off Plaid's `pending_transaction_id`. The `payment_method` label is the join key for entity-stamping, CSV supersede, and CSV-skip (the register has no account FK). Spec: `docs/superpowers/specs/2026-05-31-plaid-transactions-sync-design.md`.

**Tech Stack:** Python 3.12, SQLAlchemy, FastAPI, Alembic, plaid-python 24.0.0, pytest. Run tests with `doppler run --project accounting --config dev -- .venv/bin/python -m pytest`.

---

## Conventions for every task

- **Decimal at boundary:** `Decimal(str(value))`, never `Decimal(float)`.
- **Run tests:** prefix `doppler run --project accounting --config dev -- .venv/bin/python -m pytest`.
- **Quality gates before each commit:** `.venv/bin/python -m ruff check src/ && .venv/bin/python -m mypy src/`.
- **Commit** after each task with the message shown.
- Tests are co-located: `src/adapters/test_plaid_transactions.py`, etc.

---

## Task 1: Add enum values (Source.PLAID, depository Broker/AccountType)

**Files:**
- Modify: `src/models/enums.py`
- Test: `src/models/test_models.py`

- [ ] **Step 1: Write the failing test**

Append to `src/models/test_models.py`:

```python
def test_source_has_plaid_value():
    from src.models.enums import Source
    assert Source.PLAID.value == "plaid"


def test_account_enums_admit_chase_checking():
    from src.models.enums import AccountType, Broker
    assert Broker.CHASE.value == "chase"
    assert AccountType.CHECKING.value == "checking"
    assert AccountType.SAVINGS.value == "savings"
```

- [ ] **Step 2: Run to verify it fails**

Run: `doppler run --project accounting --config dev -- .venv/bin/python -m pytest src/models/test_models.py::test_source_has_plaid_value src/models/test_models.py::test_account_enums_admit_chase_checking -v`
Expected: FAIL — `AttributeError: PLAID` / `CHASE`.

- [ ] **Step 3: Implement**

In `src/models/enums.py`, add to `Source` (after `WOOCOMMERCE_CSV`):

```python
    PLAID = "plaid"
```

Add to `Broker` enum (find `class Broker`), a new member:

```python
    CHASE = "chase"
```

Add to `AccountType` enum:

```python
    CHECKING = "checking"
    SAVINGS = "savings"
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/models/enums.py src/models/test_models.py
git commit -m "feat(plaid): add Source.PLAID + Chase/checking/savings enums (REQ-PT-001,017)"
```

---

## Task 2: Migration — Account.payment_method + Broker/AccountType CHECK extension

**Files:**
- Modify: `src/models/brokerage.py` (add column to model)
- Create: `src/db/migrations/versions/<rev>_plaid_tx_account_payment_method.py` (via autogenerate, then hand-edit)
- Test: `src/models/test_brokerage_models.py`

> Use the `alembic-migration` skill to author and validate. CHECK-constraint string values must be enum **values** (`'chase'`, `'checking'`), not member names.

- [ ] **Step 1: Write the failing test**

Append to `src/models/test_brokerage_models.py`:

```python
def test_account_has_payment_method_label(db):
    from src.models.brokerage import Account
    acct = Account(
        broker="chase", account_number="****1234", account_name="Sparkry Operating",
        account_type="checking", entity="sparkry", payment_method="Chase ****1234",
    )
    db.add(acct); db.commit()
    assert acct.payment_method == "Chase ****1234"
```

- [ ] **Step 2: Run to verify it fails**

Run: `doppler run --project accounting --config dev -- .venv/bin/python -m pytest src/models/test_brokerage_models.py::test_account_has_payment_method_label -v`
Expected: FAIL — `TypeError: 'payment_method' is an invalid keyword argument` (and/or CHECK violation on `broker='chase'`).

- [ ] **Step 3a: Add the model column**

In `src/models/brokerage.py`, inside `class Account`, after the `plaid_account_id` column:

```python
    payment_method: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Label joining this account to register rows (e.g. 'Chase ****1234'). "
                "Join key for Plaid entity-stamp, CSV supersede, and CSV-skip.",
    )
```

Also extend the `broker` and `account_type` CHECK constraints in `__table_args__` to include the new enum values (they render from the enum, so confirm `_ENTITY_VALUES`-style join lists pick up `Broker`/`AccountType` automatically; if values are hard-coded, add `'chase'`, `'checking'`, `'savings'`).

- [ ] **Step 3b: Generate + hand-edit the migration**

```bash
doppler run --project accounting --config dev -- .venv/bin/python -m alembic revision --autogenerate -m "plaid tx: account.payment_method + chase/depository checks"
```

Open the generated file. Ensure `upgrade()` contains:

```python
def upgrade() -> None:
    op.add_column("account", sa.Column("payment_method", sa.String(length=64), nullable=True))
    # Recreate broker/account_type CHECK constraints with new allowed values.
    with op.batch_alter_table("account") as batch:
        batch.drop_constraint("ck_account_broker", type_="check")
        batch.create_check_constraint("ck_account_broker", "broker IN ('schwab','fidelity','etrade','vanguard','fg','nw_mutual','gsk','ft','chase')")
        batch.drop_constraint("ck_account_account_type", type_="check")
        batch.create_check_constraint("ck_account_account_type", "account_type IN ('brokerage','ira','roth_ira','401k','529','hsa','pension','annuity','whole_life','checking','savings')")
```

> Verify the EXACT existing constraint names and current value lists with:
> `sqlite3 data/accounting.db ".schema account"` — copy the existing IN-lists verbatim and append the new values. Do not guess the existing members.

Ensure `downgrade()` reverses in order (recreate old CHECKs, then `op.drop_column("account", "payment_method")`).

- [ ] **Step 4: Apply + run test**

```bash
doppler run --project accounting --config dev -- .venv/bin/python -m alembic upgrade head
doppler run --project accounting --config dev -- .venv/bin/python -m pytest src/models/test_brokerage_models.py::test_account_has_payment_method_label -v
```
Expected: PASS. Also run `alembic downgrade -1 && alembic upgrade head` once to prove reversibility, then leave at head.

- [ ] **Step 5: Commit**

```bash
git add src/models/brokerage.py src/db/migrations/versions/ src/models/test_brokerage_models.py
git commit -m "feat(plaid): Account.payment_method label + depository CHECK constraints (REQ-PT-017)"
```

---

## Task 3: Transaction builder helper (sign, Decimal, hash, description)

**Files:**
- Create: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

Create `src/adapters/test_plaid_transactions.py`:

```python
from decimal import Decimal
from types import SimpleNamespace

from src.adapters.plaid_transactions import build_tx_fields


def _plaid_txn(**kw):
    base = dict(
        transaction_id="txn_1", account_id="acc_1", amount=12.34, date="2026-05-01",
        name="STARBUCKS #123", merchant_name="Starbucks", pending=False,
        pending_transaction_id=None, iso_currency_code="USD",
    )
    base.update(kw)
    return SimpleNamespace(**base, to_dict=lambda: {**base})


def test_outflow_is_negative_expense():
    f = build_tx_fields(_plaid_txn(amount=12.34))
    assert f["amount"] == Decimal("-12.34")  # Plaid +outflow → DB expense (negative)


def test_inflow_is_positive_income():
    f = build_tx_fields(_plaid_txn(amount=-500.00))
    assert f["amount"] == Decimal("500.00")  # Plaid -inflow → DB income (positive)


def test_description_prefers_merchant_name():
    assert build_tx_fields(_plaid_txn())["description"] == "Starbucks"
    assert build_tx_fields(_plaid_txn(merchant_name=None))["description"] == "STARBUCKS #123"


def test_source_and_hash_stable():
    from src.utils.dedup import compute_source_hash
    f = build_tx_fields(_plaid_txn(transaction_id="txn_xyz"))
    assert f["source"] == "plaid"
    assert f["source_id"] == "txn_xyz"
    assert f["source_hash"] == compute_source_hash("plaid", "txn_xyz")
    assert f["raw_data"]["transaction_id"] == "txn_xyz"
```

- [ ] **Step 2: Run to verify it fails**

Run: `doppler run --project accounting --config dev -- .venv/bin/python -m pytest src/adapters/test_plaid_transactions.py -v`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: build_tx_fields`.

- [ ] **Step 3: Implement**

Create `src/adapters/plaid_transactions.py`:

```python
"""Plaid Transactions sync — REQ-PT-001..016.

Mirrors src/adapters/plaid_balance.py: DRY-RUN default, sync_one_item /
sync_all_active, three layers of error isolation. Cursor-based
/transactions/sync handles added/modified/removed; pending→posted reconcile
keys off Plaid's pending_transaction_id. payment_method is the join key for
entity-stamp, CSV supersede, and CSV-skip (the register has no account FK).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

SOURCE = "plaid"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_tx_fields(plaid_txn: Any) -> dict[str, Any]:
    """Map a Plaid transaction object to register-Transaction field kwargs.

    Sign: Plaid depository convention is positive = money out. DB convention is
    expense negative / income positive, so db_amount = -plaid_amount.
    """
    txn_id = plaid_txn.transaction_id
    amount = Decimal(str(-plaid_txn.amount))
    description = getattr(plaid_txn, "merchant_name", None) or plaid_txn.name
    return {
        "source": SOURCE,
        "source_id": txn_id,
        "source_hash": compute_source_hash(SOURCE, txn_id),
        "date": str(plaid_txn.date),
        "description": description,
        "amount": amount,
        "currency": "USD",
        "raw_data": plaid_txn.to_dict(),
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): transaction field builder — sign map + hash (REQ-PT-008)"
```

---

## Task 4: Classify + apply (entity from account, payment_method stamp)

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
import unittest.mock as mock
from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.adapters.plaid_transactions import make_transaction


def _cls(confidence=0.95):
    return ClassificationResult(
        entity=Entity.PERSONAL, tax_category=TaxCategory.MEALS, direction=Direction.EXPENSE,
        confidence=confidence, tier_used=1, reasoning="rule",
        status=TransactionStatus.AUTO_CLASSIFIED, deductible_pct=0.5,
    )


def test_make_transaction_entity_from_account_overrides_classifier(db):
    txn = _plaid_txn()
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="sparkry", payment_method="Chase ****1234")
    assert tx.entity == "sparkry"               # account wins over classifier's PERSONAL
    assert tx.payment_method == "Chase ****1234"
    assert tx.tax_category == TaxCategory.MEALS.value
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_make_transaction_low_confidence_needs_review(db):
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls(confidence=0.4)):
        tx = make_transaction(_plaid_txn(), session=db, entity="sparkry",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value


def test_make_transaction_unmapped_account_null_entity(db):
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(_plaid_txn(), session=db, entity=None, payment_method=None)
    assert tx.entity is None
    assert tx.payment_method is None
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py -k make_transaction -v`
Expected: FAIL — `ImportError: make_transaction`.

- [ ] **Step 3: Implement**

Add imports + function to `src/adapters/plaid_transactions.py`:

```python
from sqlalchemy.orm import Session

from src.classification.engine import classify
from src.models.enums import TransactionStatus
from src.models.transaction import Transaction

_AUTO_THRESHOLD = 0.7


def make_transaction(
    plaid_txn: Any, *, session: Session, entity: str | None, payment_method: str | None
) -> Transaction:
    """Build a classified Transaction. Entity is authoritative from the mapped
    account (overrides the classifier). Unmapped (entity None) → needs_review."""
    fields = build_tx_fields(plaid_txn)
    tx = Transaction(
        **fields, entity=entity, payment_method=payment_method, confidence=0.0,
        status=TransactionStatus.NEEDS_REVIEW.value,
    )
    result = classify(tx, session)
    tx.tax_category = result.tax_category.value
    tx.tax_subcategory = result.tax_subcategory
    tx.direction = result.direction.value
    tx.deductible_pct = result.deductible_pct
    tx.confidence = result.confidence
    tx.review_reason = result.review_reason
    # Account entity is authoritative; classifier's entity guess is discarded.
    tx.entity = entity
    needs_review = entity is None or result.confidence < _AUTO_THRESHOLD
    tx.status = (
        TransactionStatus.NEEDS_REVIEW.value if needs_review
        else TransactionStatus.AUTO_CLASSIFIED.value
    )
    if entity is None:
        tx.review_reason = "plaid: account not mapped to an entity"
    return tx
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): classify + apply, entity-from-account, needs_review routing (REQ-PT-009,010)"
```

---

## Task 5: `added` upsert + idempotency

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

Append (helper to make a mapped account + item):

```python
import pytest
from src.models.brokerage import Account
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction


def _mapped(db, plaid_account_id="acc_1", entity="sparkry", pm="Chase ****1234"):
    item = PlaidItem(item_id="it_1", institution_id="ins_56", institution_name="Chase",
                     access_token_encrypted="REVOKED", status="active")
    db.add(item); db.flush()
    acct = Account(broker="chase", account_number="****1234", account_name="Op",
                   account_type="checking", entity=entity, payment_method=pm,
                   plaid_item_id=item.id, plaid_account_id=plaid_account_id)
    db.add(acct); db.commit()
    return item, acct


def test_added_inserts_one_row_idempotent(db):
    item, acct = _mapped(db)
    txns = [_plaid_txn(transaction_id="t1", account_id="acc_1")]
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, txns, account_index={"acc_1": acct})
        process_added(db, item, txns, account_index={"acc_1": acct})  # re-run
    rows = db.query(Transaction).filter_by(source="plaid", source_id="t1").all()
    assert len(rows) == 1
    assert rows[0].entity == "sparkry"
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py::test_added_inserts_one_row_idempotent -v`
Expected: FAIL — `ImportError: process_added`.

- [ ] **Step 3: Implement**

```python
def _existing_by_source_id(session: Session, source_id: str) -> Transaction | None:
    return (
        session.query(Transaction)
        .filter(Transaction.source == SOURCE, Transaction.source_id == source_id)
        .first()
    )


def process_added(
    session: Session, item: PlaidItem, added: list[Any], *, account_index: dict[str, Account]
) -> int:
    """Insert added txns; idempotent on (source, source_id). Returns inserted count.

    Pending→posted reconcile (REQ-PT-005) is handled here too: see process_added
    in Task 6 which extends this with pending_transaction_id lookup."""
    inserted = 0
    for ptxn in added:
        if _existing_by_source_id(session, ptxn.transaction_id) is not None:
            continue
        acct = account_index.get(ptxn.account_id)
        entity = acct.entity if acct else None
        pm = acct.payment_method if acct else None
        tx = make_transaction(ptxn, session=session, entity=entity, payment_method=pm)
        session.add(tx)
        session.flush()
        inserted += 1
    return inserted
```

Add import at top: `from src.models.brokerage import Account` and `from src.models.plaid import PlaidItem`.

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): added-transaction upsert, idempotent on source_id (REQ-PT-002)"
```

---

## Task 6: Pending → posted reconcile

**Files:**
- Modify: `src/adapters/plaid_transactions.py` (extend `process_added`)
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_pending_then_posted_updates_in_place(db):
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="p1", amount=20.00, pending=True)
    posted = _plaid_txn(transaction_id="post1", amount=22.50, pending=False,
                        pending_transaction_id="p1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
        process_added(db, item, [posted], account_index={"acc_1": acct})
    rows = db.query(Transaction).filter_by(source="plaid").all()
    assert len(rows) == 1                       # no duplicate
    assert rows[0].source_id == "post1"         # promoted to posted id
    assert rows[0].amount == Decimal("-22.50")  # refreshed amount
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py::test_pending_then_posted_updates_in_place -v`
Expected: FAIL — two rows / wrong source_id.

- [ ] **Step 3: Implement**

Replace the body of `process_added`'s loop to check `pending_transaction_id` first:

```python
def process_added(
    session: Session, item: PlaidItem, added: list[Any], *, account_index: dict[str, Account]
) -> int:
    inserted = 0
    for ptxn in added:
        if _existing_by_source_id(session, ptxn.transaction_id) is not None:
            continue
        # Pending→posted: a posted txn linking to a pending row we already have.
        pending_id = getattr(ptxn, "pending_transaction_id", None)
        if pending_id:
            prior = _existing_by_source_id(session, pending_id)
            if prior is not None:
                _apply_update(prior, ptxn)
                prior.source_id = ptxn.transaction_id
                prior.source_hash = compute_source_hash(SOURCE, ptxn.transaction_id)
                session.flush()
                continue
        acct = account_index.get(ptxn.account_id)
        entity = acct.entity if acct else None
        pm = acct.payment_method if acct else None
        tx = make_transaction(ptxn, session=session, entity=entity, payment_method=pm)
        session.add(tx)
        session.flush()
        inserted += 1
    return inserted


def _apply_update(tx: Transaction, ptxn: Any) -> None:
    """Refresh volatile fields from a modified/posted Plaid txn. Preserves human
    classification — see Task 7 for the confirmed-row guard."""
    fields = build_tx_fields(ptxn)
    tx.amount = fields["amount"]
    tx.date = fields["date"]
    tx.description = fields["description"]
    tx.raw_data = fields["raw_data"]
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS. Also re-run the whole file to confirm no regression.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): pending→posted reconcile, no duplicate row (REQ-PT-005)"
```

---

## Task 7: `modified` + human-edit preservation

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_modified_updates_amount_but_preserves_confirmed_classification(db):
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="m1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="m1").one()
    row.status = "confirmed"; row.tax_category = "office_expense"; row.entity = "blackline"
    db.commit()
    process_modified(db, [_plaid_txn(transaction_id="m1", amount=11.5)])
    db.refresh(row)
    assert row.amount == Decimal("-11.50")          # volatile field refreshed
    assert row.tax_category == "office_expense"      # human edit preserved
    assert row.entity == "blackline"                 # human edit preserved
    assert row.status == "confirmed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py::test_modified_updates_amount_but_preserves_confirmed_classification -v`
Expected: FAIL — `ImportError: process_modified`.

- [ ] **Step 3: Implement**

```python
_HUMAN_LOCKED_STATUSES = {"confirmed", "edited", "rejected"}


def process_modified(session: Session, modified: list[Any]) -> int:
    """Refresh volatile fields on existing rows. Never overwrites human
    classification on locked rows; volatile (amount/date/desc/raw_data) always
    refreshes so the ledger matches the bank."""
    updated = 0
    for ptxn in modified:
        row = _existing_by_source_id(session, ptxn.transaction_id)
        if row is None:
            continue
        _apply_update(row, ptxn)
        session.flush()
        updated += 1
    return updated
```

> `_apply_update` already touches only volatile fields (amount/date/description/raw_data), so confirmed classification (entity/tax_category/direction) is preserved automatically — that is the human-edit guard. No status mutation on locked rows.

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): modified handler preserves human classification (REQ-PT-003,013)"
```

---

## Task 8: `removed` → rejected

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_removed_marks_rejected_not_deleted(db):
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="r1")],
                      account_index={"acc_1": acct})
    process_removed(db, [{"transaction_id": "r1"}])
    row = db.query(Transaction).filter_by(source_id="r1").one()  # still present
    assert row.status == "rejected"
    assert row.review_reason == "plaid_removed"


def test_removed_unknown_id_is_noop(db):
    assert process_removed(db, [{"transaction_id": "ghost"}]) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py -k process_removed -v`
Expected: FAIL — `ImportError: process_removed`.

- [ ] **Step 3: Implement**

```python
def process_removed(session: Session, removed: list[Any]) -> int:
    """Plaid removed a txn (e.g. a settled pending). Mark rejected, never delete
    (audit rule). No-op when already reconciled away or never seen."""
    count = 0
    for r in removed:
        rid = r["transaction_id"] if isinstance(r, dict) else r.transaction_id
        row = _existing_by_source_id(session, rid)
        if row is None:
            continue
        row.status = "rejected"
        row.review_reason = "plaid_removed"
        session.flush()
        count += 1
    return count
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): removed→rejected, audit-preserving (REQ-PT-004)"
```

---

## Task 9: Cursor loop + persistence + crash idempotency

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def _sync_resp(added=(), modified=(), removed=(), next_cursor="c1", has_more=False):
    return SimpleNamespace(added=list(added), modified=list(modified),
                           removed=list(removed), next_cursor=next_cursor, has_more=has_more)


def test_fetch_all_pages_concatenates_until_has_more_false(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="a")], next_cursor="c1", has_more=True),
        _sync_resp(added=[_plaid_txn(transaction_id="b")], next_cursor="c2", has_more=False),
    ]
    added, modified, removed, cursor = fetch_all_pages(client, "tok", cursor=None)
    assert [t.transaction_id for t in added] == ["a", "b"]
    assert cursor == "c2"
    assert client.transactions_sync.call_count == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py::test_fetch_all_pages_concatenates_until_has_more_false -v`
Expected: FAIL — `ImportError: fetch_all_pages`.

- [ ] **Step 3: Implement**

```python
def _sync_request(access_token: str, cursor: str | None) -> Any:
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    if cursor:
        return TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    return TransactionsSyncRequest(access_token=access_token)


def fetch_all_pages(
    client: Any, access_token: str, *, cursor: str | None
) -> tuple[list[Any], list[Any], list[Any], str]:
    """Loop /transactions/sync until has_more is False. Returns
    (added, modified, removed, next_cursor)."""
    from src.adapters.plaid_client import call_with_retry
    added: list[Any] = []
    modified: list[Any] = []
    removed: list[Any] = []
    while True:
        resp = call_with_retry(lambda: client.transactions_sync(_sync_request(access_token, cursor)))
        added += list(resp.added)
        modified += list(resp.modified)
        removed += list(resp.removed)
        cursor = resp.next_cursor
        if not resp.has_more:
            break
    return added, modified, removed, cursor
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): cursor-paged /transactions/sync fetch loop (REQ-PT-001,006)"
```

---

## Task 10: CSV supersede on first sync

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_supersede_rejects_overlapping_csv_rows_only(db):
    item, acct = _mapped(db, pm="Chase ****1234")
    # Existing CSV rows: one in range (same label), one out of range, one other label.
    db.add(Transaction(source="bank_csv", source_id="c1", source_hash="h1", date="2026-03-15",
                       description="x", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Chase ****1234"))
    db.add(Transaction(source="bank_csv", source_id="c2", source_hash="h2", date="2025-01-01",
                       description="old", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Chase ****1234"))
    db.add(Transaction(source="bank_csv", source_id="c3", source_hash="h3", date="2026-03-15",
                       description="other", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Amex ****9999"))
    db.commit()
    n = supersede_csv_rows(db, payment_method="Chase ****1234",
                           covered_min="2026-01-01", covered_max="2026-05-31")
    assert n == 1
    assert db.query(Transaction).filter_by(source_id="c1").one().status == "rejected"
    assert db.query(Transaction).filter_by(source_id="c2").one().status == "confirmed"  # out of range
    assert db.query(Transaction).filter_by(source_id="c3").one().status == "confirmed"  # other label


def test_supersede_noop_when_label_blank(db):
    assert supersede_csv_rows(db, payment_method=None,
                              covered_min="2026-01-01", covered_max="2026-05-31") == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py -k supersede -v`
Expected: FAIL — `ImportError: supersede_csv_rows`.

- [ ] **Step 3: Implement**

```python
def supersede_csv_rows(
    session: Session, *, payment_method: str | None, covered_min: str, covered_max: str
) -> int:
    """Mark non-Plaid rows for this payment_method label, within Plaid's covered
    date range, as rejected (superseded). Audit rule: never delete. A blank label
    disables supersede (returns 0, logged)."""
    if not payment_method:
        logger.warning("plaid supersede skipped: account has no payment_method label")
        return 0
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.source != SOURCE,
            Transaction.payment_method == payment_method,
            Transaction.date >= covered_min,
            Transaction.date <= covered_max,
            Transaction.status != "rejected",
        )
        .all()
    )
    for row in rows:
        row.status = "rejected"
        row.review_reason = "superseded_by_plaid"
    session.flush()
    return len(rows)
```

> Date columns are ISO `YYYY-MM-DD` strings (see `Transaction.date`), so string comparison is chronological. Confirm with `.schema transactions`.

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): first-sync CSV supersede keyed on payment_method (REQ-PT-011)"
```

---

## Task 11: `sync_one_item` orchestration (savepoints + cursor + supersede + IngestionLog)

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sync_one_item_full_flow_first_sync_sets_cursor(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="t1", account_id="acc_1")],
                   next_cursor="cur1", has_more=False)
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "ok"
    assert result.added == 1
    assert db.query(Transaction).filter_by(source="plaid", source_id="t1").count() == 1
    db.refresh(item)
    assert item.cursor == "cur1"


def test_sync_one_item_per_row_isolation(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    good = _plaid_txn(transaction_id="ok", account_id="acc_1")
    bad = _plaid_txn(transaction_id="bad", account_id="acc_1")
    client.transactions_sync.side_effect = [
        _sync_resp(added=[bad, good], has_more=False, next_cursor="c")
    ]
    def flaky(txn, **kw):
        if txn.transaction_id == "bad":
            raise ValueError("boom")
        return make_transaction.__wrapped__(txn, **kw) if hasattr(make_transaction, "__wrapped__") else _cls()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", side_effect=lambda t, s: (_ for _ in ()).throw(ValueError("boom")) if t.source_id == "bad" else _cls()):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.failed == 1
    assert db.query(Transaction).filter_by(source_id="ok").count() == 1
```

> If the second test's mock plumbing is awkward in your harness, simplify: patch `process_added` to raise on the bad id. The REQ being proven is: one bad row → `result.failed >= 1`, good rows still committed, batch does not abort.

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py::test_sync_one_item_full_flow_first_sync_sets_cursor -v`
Expected: FAIL — `ImportError: sync_one_item`.

- [ ] **Step 3: Implement**

```python
from src.adapters.plaid_client import (
    PlaidErrorBase, RetryablePlaidError, TerminalPlaidError,
)
from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog
from src.utils.plaid_crypto import InvalidCiphertextError, decrypt_token


@dataclass
class TxItemResult:
    item_id: str
    institution_name: str
    status: str = "ok"          # 'ok' | 'error' | 'institution_down'
    added: int = 0
    modified: int = 0
    removed: int = 0
    failed: int = 0
    superseded: int = 0
    error_code: str | None = None


def sync_one_item(session: Session, item: PlaidItem, *, client: Any) -> TxItemResult:
    """Sync one Item's transactions. Caller owns the outer commit."""
    result = TxItemResult(item_id=item.id, institution_name=item.institution_name)
    pulled_at = _utcnow()
    log_row = IngestionLog(source=f"plaid_tx:{item.institution_name}",
                           status=IngestionStatus.SUCCESS.value, run_at=pulled_at)
    session.add(log_row)
    first_sync = item.cursor is None

    # Index this item's mapped accounts by plaid_account_id.
    accounts = session.query(Account).filter_by(plaid_item_id=item.id).all()
    account_index = {a.plaid_account_id: a for a in accounts if a.plaid_account_id}

    try:
        try:
            access_token = decrypt_token(item.access_token_encrypted)
        except InvalidCiphertextError as exc:
            raise TerminalPlaidError("INVALID_ACCESS_TOKEN",
                                     message="cannot decrypt token") from exc

        added, modified, removed, next_cursor = fetch_all_pages(
            client, access_token, cursor=item.cursor
        )

        for ptxn in added:
            try:
                with session.begin_nested():
                    result.added += process_added(session, item, [ptxn],
                                                   account_index=account_index)
            except Exception:
                result.failed += 1
                logger.exception("plaid tx added failure",
                                 extra={"plaid_item_id": item.id,
                                        "txn": getattr(ptxn, "transaction_id", "?")})
        for ptxn in modified:
            try:
                with session.begin_nested():
                    result.modified += process_modified(session, [ptxn])
            except Exception:
                result.failed += 1
        try:
            with session.begin_nested():
                result.removed += process_removed(session, removed)
        except Exception:
            result.failed += 1

        if first_sync and added:
            dates = [str(t.date) for t in added]
            for acct in accounts:
                result.superseded += supersede_csv_rows(
                    session, payment_method=acct.payment_method,
                    covered_min=min(dates), covered_max=max(dates),
                )

        # Advance cursor ONLY after a clean page-loop (REQ-PT-006).
        item.cursor = next_cursor
        item.last_sync_at = pulled_at
        item.last_sync_status = "ok"
        item.last_error = None
        log_row.records_processed = result.added + result.modified + result.removed
        log_row.records_failed = result.failed
        log_row.status = (IngestionStatus.PARTIAL_FAILURE.value if result.failed
                          else IngestionStatus.SUCCESS.value)

    except RetryablePlaidError as exc:
        item.last_sync_status = ("institution_down"
            if exc.error_code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING") else "error")
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = True
        log_row.error_detail = exc.error_code
        result.status = item.last_sync_status
        result.error_code = exc.error_code
    except (TerminalPlaidError, PlaidErrorBase) as exc:
        item.last_sync_status = "error"
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = exc.error_code
        result.status = "error"
        result.error_code = exc.error_code
    except Exception as exc:
        item.last_sync_status = "error"
        item.last_error = "UNEXPECTED"
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"unexpected: {type(exc).__name__}"
        result.status = "error"
        result.error_code = "UNEXPECTED"
        logger.exception("plaid tx per-item failure", extra={"plaid_item_id": item.id})

    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `... pytest src/adapters/test_plaid_transactions.py -v`
Expected: PASS (all tests in file).

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): sync_one_item orchestration — cursor, savepoints, supersede, log (REQ-PT-001,006,007,011,016)"
```

---

## Task 12: `sync_all_active` batch driver (DRY-RUN default)

**Files:**
- Modify: `src/adapters/plaid_transactions.py`
- Test: `src/adapters/test_plaid_transactions.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sync_all_active_dry_run_rolls_back(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="d1", account_id="acc_1")], has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        batch = sync_all_active(db, client=client, dry_run=True)
    assert batch.dry_run is True
    assert db.query(Transaction).filter_by(source_id="d1").count() == 0  # rolled back


def test_sync_all_active_apply_commits(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="a1", account_id="acc_1")], has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        sync_all_active(db, client=client, dry_run=False)
    assert db.query(Transaction).filter_by(source_id="a1").count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_plaid_transactions.py -k sync_all_active -v`
Expected: FAIL — `ImportError: sync_all_active`.

- [ ] **Step 3: Implement**

```python
@dataclass
class TxBatchResult:
    items: list[TxItemResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_added(self) -> int:
        return sum(i.added for i in self.items)


def sync_all_active(session: Session, *, client: Any, dry_run: bool = True) -> TxBatchResult:
    """Sync transactions for every active PlaidItem. DRY-RUN default."""
    batch = TxBatchResult(dry_run=dry_run)
    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status == "active", ~PlaidItem.item_id.like("placeholder_%"))
        .all()
    )
    for item in items:
        batch.items.append(sync_one_item(session, item, client=client))
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return batch
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/plaid_transactions.py src/adapters/test_plaid_transactions.py
git commit -m "feat(plaid): sync_all_active batch driver, DRY-RUN default (REQ-PT-001)"
```

---

## Task 13: bank_csv sole-source skip

**Files:**
- Modify: `src/adapters/bank_csv.py` (`_process_row`)
- Test: `src/adapters/test_bank_csv.py`

- [ ] **Step 1: Write the failing test**

Append to `src/adapters/test_bank_csv.py` (follow the file's existing fixture style for building a `BankCsvAdapter` + config; the assertion is the new behavior):

```python
def test_bank_csv_skips_plaid_owned_payment_method(db):
    from src.models.brokerage import Account
    from src.models.plaid import PlaidItem
    item = PlaidItem(item_id="it", institution_id="ins", institution_name="Chase",
                     access_token_encrypted="REVOKED", status="active")
    db.add(item); db.flush()
    db.add(Account(broker="chase", account_number="****1", account_name="op",
                   account_type="checking", entity="sparkry",
                   payment_method="Chase ****1234", plaid_item_id=item.id,
                   plaid_account_id="acc_1"))
    db.commit()
    # Build an adapter whose config.payment_method == "Chase ****1234" with one data row,
    # run it with dry_run=False, and assert nothing was created and the row was counted skipped.
    result = _run_bank_csv(db, payment_method="Chase ****1234", rows=[("2026-05-01", "-10.00", "coffee")])
    assert result.records_created == 0
    assert result.records_skipped >= 1
```

> `_run_bank_csv` is a thin local helper you add mirroring the file's existing CSV-building tests. If the file already has such a helper, reuse it.

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/adapters/test_bank_csv.py::test_bank_csv_skips_plaid_owned_payment_method -v`
Expected: FAIL — a row is created (skip not implemented).

- [ ] **Step 3: Implement**

In `src/adapters/bank_csv.py`, add a helper near the top-level functions:

```python
def _is_plaid_owned(session: Session, payment_method: str | None) -> bool:
    """True if a payment_method label belongs to a Plaid-linked account — bank CSV
    must not re-ingest it (Plaid is sole source). REQ-PT-012."""
    if not payment_method:
        return False
    from src.models.brokerage import Account
    return (
        session.query(Account.id)
        .filter(Account.payment_method == payment_method,
                Account.plaid_item_id.isnot(None))
        .first()
        is not None
    )
```

At the very top of `_process_row` (before computing the hash), add:

```python
        if _is_plaid_owned(session, self._config.payment_method):
            result.records_skipped += 1
            return
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Then run the full bank_csv suite: `... pytest src/adapters/test_bank_csv.py -v`.
Expected: PASS, no regression.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/bank_csv.py src/adapters/test_bank_csv.py
git commit -m "feat(plaid): bank_csv skips Plaid-owned payment_method (REQ-PT-012)"
```

---

## Task 14: map-accounts accepts payment_method

**Files:**
- Modify: `src/api/routes/plaid.py` (`MapAccountsRequest` models + `map_accounts`)
- Test: `src/api/test_plaid_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `src/api/test_plaid_routes.py` (mirror `test_map_accounts_writes_audit_per_mapping` setup):

```python
def test_map_accounts_persists_payment_method(client, db):
    # Arrange an active item with a known id (reuse the file's helper that creates one).
    item = _make_active_item(db)  # existing helper in this test file
    resp = client.post("/api/plaid/map-accounts", json={
        "item_id": item.id,
        "mappings": [{
            "plaid_account_id": "acc_1",
            "create_new": {"broker": "chase", "account_number": "****1234",
                           "account_name": "Sparkry Operating", "account_type": "checking",
                           "entity": "sparkry", "tax_sheltered": False,
                           "payment_method": "Chase ****1234"},
        }],
    })
    assert resp.status_code == 200
    from src.models.brokerage import Account
    acct = db.query(Account).filter_by(plaid_account_id="acc_1").one()
    assert acct.payment_method == "Chase ****1234"
```

> If the file lacks `_make_active_item`, create the `PlaidItem` inline as in other tests.

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/api/test_plaid_routes.py::test_map_accounts_persists_payment_method -v`
Expected: FAIL — `payment_method` not accepted / not persisted.

- [ ] **Step 3: Implement**

In `src/api/routes/plaid.py`, find the `create_new` Pydantic model (the `CreateAccountSpec`-style class inside `MapAccountsRequest`) and add:

```python
    payment_method: str | None = None
```

In `map_accounts`, in the `create_new` branch, pass it through:

```python
            account = Account(
                broker=m.create_new.broker,
                account_number=m.create_new.account_number,
                account_name=m.create_new.account_name,
                account_type=m.create_new.account_type,
                entity=m.create_new.entity,
                tax_sheltered=m.create_new.tax_sheltered,
                payment_method=m.create_new.payment_method,
            )
```

And in the existing-account branch (`if m.account_id:`), after fetching `account`, set the label if provided:

```python
        if getattr(m, "payment_method", None):
            account.payment_method = m.payment_method
```

(Add `payment_method: str | None = None` to the per-mapping model too, for the existing-account path.)

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2, then full `... pytest src/api/test_plaid_routes.py -v`.
Expected: PASS, 48+ tests.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/plaid.py src/api/test_plaid_routes.py
git commit -m "feat(plaid): map-accounts persists payment_method label (REQ-PT-017)"
```

---

## Task 15: CLI wrapper script

**Files:**
- Create: `scripts/plaid_transactions_sync.py`
- Test: `scripts/test_plaid_transactions_sync.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/test_plaid_transactions_sync.py`:

```python
import unittest.mock as mock
from scripts import plaid_transactions_sync as cli


def test_main_dry_run_default_does_not_apply():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_added=0, dry_run=True)
        cli.main([])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is True


def test_main_apply_flag_writes():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_added=0, dry_run=False)
        cli.main(["--apply"])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest scripts/test_plaid_transactions_sync.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `scripts/plaid_transactions_sync.py` (model on `scripts/plaid_balance_sync.py`):

```python
"""Plaid Transactions daily sync — CLI wrapper around src.adapters.plaid_transactions.

DRY-RUN by default; pass --apply to commit. Designed for launchd
(com.sparkry.plaid-transactions-sync.plist).

    doppler run -- python -m scripts.plaid_transactions_sync           # dry-run
    doppler run -- python -m scripts.plaid_transactions_sync --apply   # commit
"""

from __future__ import annotations

import argparse
import logging
import sys

from src.adapters.plaid_client import make_plaid_client
from src.adapters.plaid_transactions import sync_all_active
from src.db.connection import SessionLocal

logger = logging.getLogger("plaid_transactions_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plaid transactions sync")
    parser.add_argument("--apply", action="store_true", help="commit (default dry-run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    client = make_plaid_client()
    session = SessionLocal()
    try:
        batch = sync_all_active(session, client=client, dry_run=not args.apply)
        logger.info("plaid tx sync %s: items=%d added=%d",
                    "APPLIED" if args.apply else "DRY-RUN",
                    len(batch.items), batch.total_added)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/plaid_transactions_sync.py scripts/test_plaid_transactions_sync.py
git commit -m "feat(plaid): launchd CLI wrapper for transactions sync (REQ-PT-014)"
```

---

## Task 16: launchd plist (daily schedule)

**Files:**
- Create: `com.sparkry.plaid-transactions-sync.plist`

- [ ] **Step 1: Create the plist**

Copy `com.sparkry.plaid-balance-sync.plist` if present (else `com.sparkry.accounting-prices-daily.plist`) and adapt. It must run `doppler run --project accounting --config dev -- .venv/bin/python -m scripts.plaid_transactions_sync --apply`, with `StandardOutPath`/`StandardErrorPath` under `~/Library/Logs/`, and a `StartCalendarInterval` ~30 min after the balance-sync hour to avoid Plaid rate contention.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.sparkry.plaid-transactions-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>cd /Users/travis/SGDrive/dev/accounting &amp;&amp; doppler run --project accounting --config dev -- .venv/bin/python -m scripts.plaid_transactions_sync --apply</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/Users/travis/Library/Logs/com.sparkry.plaid-transactions-sync.log</string>
  <key>StandardErrorPath</key><string>/Users/travis/Library/Logs/com.sparkry.plaid-transactions-sync.error.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
```

- [ ] **Step 2: Validate (do not load until production Plaid is live)**

Run: `plutil -lint com.sparkry.plaid-transactions-sync.plist`
Expected: `OK`.

> **Do not `launchctl load`** until the §9 prerequisites (production Plaid + Chase OAuth) are met — until then a daily run only exercises sandbox. Loading is an ops step gated on go-live.

- [ ] **Step 3: Commit**

```bash
git add com.sparkry.plaid-transactions-sync.plist
git commit -m "feat(plaid): daily transactions-sync launchd plist (REQ-PT-014)"
```

---

## Task 17: Manual `sync-transactions` endpoint

**Files:**
- Modify: `src/api/routes/plaid.py`
- Test: `src/api/test_plaid_routes.py`

- [ ] **Step 1: Write the failing test**

```python
def test_sync_transactions_now_rate_limited(client, db, plaid_client_mock):
    item = _make_active_item(db)
    plaid_client_mock.transactions_sync.return_value = SimpleNamespace(
        added=[], modified=[], removed=[], next_cursor="c", has_more=False)
    r1 = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert r1.status_code == 200
    r2 = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert r2.status_code == 429  # cooldown
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest src/api/test_plaid_routes.py::test_sync_transactions_now_rate_limited -v`
Expected: FAIL — 404 (route missing).

- [ ] **Step 3: Implement**

In `src/api/routes/plaid.py`, add near the other module-level tunables:

```python
_tx_sync_now_last_call: dict[str, float] = {}
```

Add the route (after `sync_now`):

```python
@router.post("/items/{item_id}/sync-transactions")
def sync_transactions_now(
    item_id: str = Path(..., min_length=1),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Manual trigger of Plaid transactions sync for one Item. Rate-limited
    1/min/item (REQ-PT-015)."""
    from src.adapters.plaid_transactions import sync_one_item as _tx_sync_one

    now = time.monotonic()
    last = _tx_sync_now_last_call.get(item_id, 0.0)
    if now - last < _SYNC_NOW_COOLDOWN_SECONDS:
        wait = int(_SYNC_NOW_COOLDOWN_SECONDS - (now - last))
        raise HTTPException(status_code=429, detail=f"sync cooldown active, retry in {wait}s")
    _tx_sync_now_last_call[item_id] = now

    item = session.query(PlaidItem).filter_by(id=item_id, status="active").first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    client = _get_plaid_client()
    result = _tx_sync_one(session, item, client=client)
    session.commit()
    return {"status": result.status, "added": result.added, "modified": result.modified,
            "removed": result.removed, "failed": result.failed, "superseded": result.superseded,
            "error_code": result.error_code}
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2, then full `... pytest src/api/test_plaid_routes.py -v`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/plaid.py src/api/test_plaid_routes.py
git commit -m "feat(plaid): manual sync-transactions endpoint, rate-limited (REQ-PT-015)"
```

---

## Task 18: Requirements + docs

**Files:**
- Modify: `requirements/current.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add REQ-PT-001..017 to `requirements/current.md`**

Add a section "## Plaid Phase 2 — Transactions Sync (REQ-PT-*)" listing each requirement from the spec table (§3). Copy the requirement text verbatim.

- [ ] **Step 2: Update `CLAUDE.md`**

- Under the Plaid bullet/architecture, note that Plaid Phase 2 ingests transactions (`src/adapters/plaid_transactions.py`, `scripts/plaid_transactions_sync.py`, `/api/plaid/items/{id}/sync-transactions`), Plaid is sole-source per linked account, supersede + CSV-skip key off `payment_method`.
- Add `com.sparkry.plaid-transactions-sync.plist` to the launchd services table (note: not loaded until production Plaid + Chase OAuth are live).

- [ ] **Step 3: Full quality gates**

```bash
doppler run --project accounting --config dev -- .venv/bin/python -m pytest
.venv/bin/python -m ruff check src/ scripts/
.venv/bin/python -m mypy src/
```
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add requirements/current.md CLAUDE.md
git commit -m "docs(plaid): REQ-PT-* requirements + CLAUDE.md Phase 2 notes"
```

---

## Self-Review

**Spec coverage:** REQ-PT-001 (T9,11,12) · 002 (T5) · 003 (T7) · 004 (T8) · 005 (T6) · 006 (T9,11) · 007 (T11) · 008 (T3) · 009 (T4) · 010 (T4) · 011 (T10,11) · 012 (T13) · 013 (T7) · 014 (T15,16) · 015 (T17) · 016 (T11) · 017 (T1,2,14). All covered.

**Prerequisites (not in plan, tracked separately):** production Plaid env, Chase OAuth redirect — §9 of the spec. The launchd plist is created but explicitly NOT loaded until those are met.

**Type consistency:** `build_tx_fields` → dict; `make_transaction` → Transaction; `process_added/modified/removed` → int; `sync_one_item` → `TxItemResult`; `sync_all_active` → `TxBatchResult`. `_apply_update` shared by Tasks 6–7. Names consistent across tasks.

**Known harness caveat:** Task 11's per-row-isolation test uses intricate mock plumbing; the inline note offers the simpler `process_added`-raises approach proving the same REQ.
