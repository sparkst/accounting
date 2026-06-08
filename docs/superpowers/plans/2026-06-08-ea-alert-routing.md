# EA Alert Routing via n8n Webhook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily dispatch job that computes due WA B&O tax reminders and invoice-submission reminders, dedupes them in an audit ledger, and POSTs email-ready payloads to an n8n webhook relay.

**Architecture:** New `src/alerts/` module — pure rule functions compute `Alert` objects for a given date; a dispatcher filters already-sent alerts via the `alert_dispatch` table, POSTs each to n8n (DRY-RUN by default), and records the outcome. A CLI entrypoint is invoked by a daily systemd timer on the Hetzner box.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, httpx, pytest. Secrets via Doppler. Follows the `src/planning/` module shape.

**Spec:** `docs/superpowers/specs/2026-06-07-ea-alert-routing-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/alerts/__init__.py` | Package marker |
| `src/alerts/models.py` | `AlertDispatch` ORM model (dedup + audit ledger) |
| `src/alerts/rules.py` | `Alert` dataclass + `compute_tax_alerts` / `compute_invoice_alerts` (pure) |
| `src/alerts/webhook.py` | `WebhookResult`, `build_payload`, `post_alert` (httpx, DRY-RUN default) |
| `src/alerts/dispatcher.py` | `DispatchSummary`, `dispatch_alerts` (dedup, per-alert isolation) |
| `src/alerts/test_rules.py` | Tests for rule generation |
| `src/alerts/test_webhook.py` | Tests for payload + post behavior |
| `src/alerts/test_dispatcher.py` | Tests for dedup + isolation |
| `scripts/alerts_dispatch.py` | CLI entrypoint (DRY-RUN default, `--apply`, `--date`) |
| `src/db/alembic/versions/al0_add_alert_dispatch.py` | Migration creating `alert_dispatch` |
| `src/db/connection.py` (modify) | Register `AlertDispatch` for `create_all` |
| `src/db/alembic/env.py` (modify) | Register `AlertDispatch` for autogenerate metadata |
| `deploy/com.sparkry.alerts-dispatch.service` + `.timer` | systemd units (Hetzner) |
| `com.sparkry.alerts-dispatch.plist` | macOS LaunchAgent parity |

Alembic head at plan time: **`63c79e8be034`**.

---

## Task 1: `AlertDispatch` model

**Files:**
- Create: `src/alerts/__init__.py`
- Create: `src/alerts/models.py`
- Test: `src/alerts/test_models.py`

- [ ] **Step 1: Create the package marker**

Create `src/alerts/__init__.py`:

```python
"""EA alert routing: compute due alerts and dispatch them to the n8n email relay."""
```

- [ ] **Step 2: Write the failing test**

Create `src/alerts/test_models.py`:

```python
"""Tests for the AlertDispatch ledger model.

REQ-ID: REQ-ALERT-006 (dedup uniqueness)
REQ-ID: REQ-ALERT-010 (table shape)
"""

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.close()


def test_alert_dispatch_persists_and_autofills(session: Session) -> None:
    row = AlertDispatch(
        alert_key="tax:sparkry:bo:2026-04",
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="WA B&O due — Sparkry LLC",
        status="sent",
        http_status=200,
    )
    session.add(row)
    session.commit()
    assert row.id  # uuid autofilled
    assert row.created_at  # timestamp autofilled


def test_alert_key_occurrence_date_is_unique(session: Session) -> None:
    common = dict(
        alert_key="tax:sparkry:bo:2026-04",
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="dup",
        status="sent",
    )
    session.add(AlertDispatch(**common))
    session.commit()
    session.add(AlertDispatch(**common))
    with pytest.raises(IntegrityError):
        session.commit()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.alerts.models'`

- [ ] **Step 4: Write the model**

Create `src/alerts/models.py`:

```python
"""AlertDispatch — the dedup + audit ledger for dispatched EA alerts.

One row per (alert_key, occurrence_date) actually dispatched.  The UNIQUE
constraint is the dedup guarantee: re-running the daily job never sends the same
alert twice.  Additive table only — touches no protected table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


class AlertDispatch(Base):
    __tablename__ = "alert_dispatch"
    __table_args__ = (
        UniqueConstraint(
            "alert_key", "occurrence_date", name="uq_alert_dispatch_key_date"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    alert_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    occurrence_date: Mapped[str] = mapped_column(String(10), nullable=False)
    alert_type: Mapped[str] = mapped_column(String, nullable=False)
    entity: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # sent|failed|dry_run
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)

    def __repr__(self) -> str:
        return (
            f"<AlertDispatch {self.alert_key}@{self.occurrence_date} "
            f"status={self.status}>"
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add src/alerts/__init__.py src/alerts/models.py src/alerts/test_models.py
git commit -m "feat(alerts): AlertDispatch dedup+audit ledger model (REQ-ALERT-006/010)"
```

---

## Task 2: Register the model + Alembic migration

**Files:**
- Modify: `src/db/connection.py` (model imports block, ~line 49)
- Modify: `src/db/alembic/env.py` (model imports block, ~line 40)
- Create: `src/db/alembic/versions/al0_add_alert_dispatch.py`

- [ ] **Step 1: Register for `create_all`**

In `src/db/connection.py`, in the block of `# noqa: F401` model imports (right after the `from src.models.vendor_rule import VendorRule` line), add:

```python
from src.alerts.models import AlertDispatch  # noqa: F401
```

- [ ] **Step 2: Register for autogenerate**

In `src/db/alembic/env.py`, after `from src.models.transaction import Transaction  # noqa: F401`, add:

```python
from src.alerts.models import AlertDispatch  # noqa: F401
```

- [ ] **Step 3: Write the migration**

Create `src/db/alembic/versions/al0_add_alert_dispatch.py`:

```python
"""REQ-ALERT-010 add alert_dispatch table (EA alert dedup + audit ledger)

Revision ID: al0_add_alert_dispatch
Revises: 63c79e8be034
Create Date: 2026-06-08 00:00:00.000000

Additive only. Creates the alert_dispatch table used to dedupe and audit EA
alert emails. Touches no protected table; downgrade drops only this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "al0_add_alert_dispatch"
down_revision: str | Sequence[str] | None = "63c79e8be034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_dispatch",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_key", sa.String(), nullable=False),
        sa.Column("occurrence_date", sa.String(length=10), nullable=False),
        sa.Column("alert_type", sa.String(), nullable=False),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "alert_key", "occurrence_date", name="uq_alert_dispatch_key_date"
        ),
    )
    op.create_index(
        "ix_alert_dispatch_alert_key", "alert_dispatch", ["alert_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_alert_dispatch_alert_key", table_name="alert_dispatch")
    op.drop_table("alert_dispatch")
```

- [ ] **Step 4: Verify the migration applies and is at head**

Run:
```bash
doppler run -- alembic upgrade head
doppler run -- alembic current
```
Expected: `alembic current` shows `al0_add_alert_dispatch (head)`.

- [ ] **Step 5: Verify downgrade is clean (then re-upgrade)**

Run:
```bash
doppler run -- alembic downgrade -1
doppler run -- alembic upgrade head
```
Expected: both succeed with no error.

- [ ] **Step 6: Commit**

```bash
git add src/db/connection.py src/db/alembic/env.py src/db/alembic/versions/al0_add_alert_dispatch.py
git commit -m "feat(alerts): register AlertDispatch + alembic migration (REQ-ALERT-010)"
```

---

## Task 3: `Alert` dataclass + tax rules

**Files:**
- Create: `src/alerts/rules.py`
- Test: `src/alerts/test_rules.py`

- [ ] **Step 1: Write the failing test**

Create `src/alerts/test_rules.py`:

```python
"""Tests for alert rule generation.

REQ-ID: REQ-ALERT-001 (Sparkry monthly B&O escalation days)
REQ-ID: REQ-ALERT-002 (BlackLine quarterly B&O escalation)
REQ-ID: REQ-ALERT-003 (tax_bo body has account id, period, due date, login url)
"""

from datetime import date

from src.alerts.rules import compute_tax_alerts


def _by_key(alerts: list) -> dict:
    return {a.alert_key: a for a in alerts}


def test_sparkry_monthly_fires_on_reminder_days() -> None:
    # 2026-05-10 is a reminder day (10th); period is prior month = April 2026.
    alerts = compute_tax_alerts(date(2026, 5, 10))
    sparkry = _by_key(alerts).get("tax:sparkry:bo:2026-04")
    assert sparkry is not None
    assert sparkry.alert_type == "tax_bo"
    assert sparkry.entity == "sparkry"
    assert sparkry.due_date == "2026-05-25"
    assert sparkry.occurrence_date == "2026-05-10"
    # Body must carry the actionable context.
    assert "605-965-107" in sparkry.body_text  # DOR account
    assert "April 2026" in sparkry.body_text  # filing period
    assert "https://secure.dor.wa.gov/home/Login" in sparkry.action_url


def test_sparkry_monthly_silent_on_non_reminder_day() -> None:
    # 2026-05-11 is not in {3,10,17,25}.
    alerts = compute_tax_alerts(date(2026, 5, 11))
    assert "tax:sparkry:bo:2026-04" not in _by_key(alerts)


def test_sparkry_fires_on_due_date_25th() -> None:
    alerts = compute_tax_alerts(date(2026, 5, 25))
    assert "tax:sparkry:bo:2026-04" in _by_key(alerts)


def test_blackline_q1_fires_in_april_reminder_window() -> None:
    # Q1 (Jan-Mar) is due Apr 30; April 17 is a reminder day.
    alerts = compute_tax_alerts(date(2026, 4, 17))
    bl = _by_key(alerts).get("tax:blackline:bo:2026-Q1")
    assert bl is not None
    assert bl.entity == "blackline"
    assert bl.due_date == "2026-04-30"
    assert "605-922-410" in bl.body_text
    assert "Q1" in bl.body_text


def test_blackline_q1_fires_on_due_date_apr_30() -> None:
    alerts = compute_tax_alerts(date(2026, 4, 30))
    assert "tax:blackline:bo:2026-Q1" in _by_key(alerts)


def test_blackline_q4_uses_prior_year_in_january() -> None:
    # January 2026 due date is Jan 31 for Q4 of 2025.
    alerts = compute_tax_alerts(date(2026, 1, 10))
    bl = _by_key(alerts).get("tax:blackline:bo:2025-Q4")
    assert bl is not None
    assert bl.due_date == "2026-01-31"


def test_no_tax_alerts_on_quiet_month_day() -> None:
    # A non-due month (e.g. May) day that is not a Sparkry reminder day.
    alerts = compute_tax_alerts(date(2026, 5, 13))
    assert alerts == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.alerts.rules'`

- [ ] **Step 3: Write the `Alert` dataclass + tax rules**

Create `src/alerts/rules.py`:

```python
"""Pure alert-rule functions.

Given a date (and, for invoices, a Session), return the list of Alert objects
that should fire today.  No network, no writes — fully unit-testable by passing
a fixed `today`.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

DOR_LOGIN_URL = "https://secure.dor.wa.gov/home/Login"
SPARKRY_DOR_ACCOUNT = "605-965-107"
BLACKLINE_DOR_ACCOUNT = "605-922-410"

# Reminder days within the relevant month.
SPARKRY_REMINDER_DAYS = (3, 10, 17, 25)
BLACKLINE_REMINDER_DAYS = (3, 10, 17, 24)  # plus the due date (last day), added below


@dataclass(frozen=True)
class Alert:
    alert_key: str
    occurrence_date: str
    alert_type: str  # "tax_bo" | "invoice_sweep" | "invoice_draft"
    entity: str
    subject: str
    body_text: str
    due_date: str | None
    action_url: str
    body_html: str | None = None


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _sparkry_monthly_alert(today: date) -> Alert | None:
    if today.day not in SPARKRY_REMINDER_DAYS:
        return None
    # Filing period = the prior month.
    first_of_month = today.replace(day=1)
    prior = first_of_month - timedelta(days=1)
    period_key = prior.strftime("%Y-%m")
    period_label = prior.strftime("%B %Y")
    due = today.replace(day=25)
    body = (
        f"Sparkry LLC monthly WA B&O for {period_label} is due {due.isoformat()}.\n"
        f"DOR account {SPARKRY_DOR_ACCOUNT}.\n"
        f"File at: {DOR_LOGIN_URL}\n"
        f"(Log in, then open the {period_label} return for account "
        f"{SPARKRY_DOR_ACCOUNT}.)"
    )
    return Alert(
        alert_key=f"tax:sparkry:bo:{period_key}",
        occurrence_date=today.isoformat(),
        alert_type="tax_bo",
        entity="sparkry",
        subject=f"WA B&O due — Sparkry LLC ({period_label}) by {due.isoformat()}",
        body_text=body,
        due_date=due.isoformat(),
        action_url=DOR_LOGIN_URL,
    )


# due-month -> (quarter label, quarter number, year offset relative to today.year)
_BLACKLINE_DUE_MONTHS = {
    4: ("Q1 (Jan-Mar)", 1, 0),
    7: ("Q2 (Apr-Jun)", 2, 0),
    10: ("Q3 (Jul-Sep)", 3, 0),
    1: ("Q4 (Oct-Dec)", 4, -1),
}


def _blackline_quarterly_alert(today: date) -> Alert | None:
    meta = _BLACKLINE_DUE_MONTHS.get(today.month)
    if meta is None:
        return None
    label, qnum, year_offset = meta
    last = _last_day(today.year, today.month)
    reminder_days = set(BLACKLINE_REMINDER_DAYS) | {last}
    if today.day not in reminder_days:
        return None
    quarter_year = today.year + year_offset
    period_key = f"{quarter_year}-Q{qnum}"
    due = date(today.year, today.month, last)
    body = (
        f"BlackLine MTB Apparel LLC quarterly WA B&O for {period_key} "
        f"{label} is due {due.isoformat()}.\n"
        f"DOR account {BLACKLINE_DOR_ACCOUNT}.\n"
        f"File at: {DOR_LOGIN_URL}\n"
        f"(Log in, then open the {period_key} return for account "
        f"{BLACKLINE_DOR_ACCOUNT}.)"
    )
    return Alert(
        alert_key=f"tax:blackline:bo:{period_key}",
        occurrence_date=today.isoformat(),
        alert_type="tax_bo",
        entity="blackline",
        subject=f"WA B&O due — BlackLine ({period_key}) by {due.isoformat()}",
        body_text=body,
        due_date=due.isoformat(),
        action_url=DOR_LOGIN_URL,
    )


def compute_tax_alerts(today: date) -> list[Alert]:
    """Return all WA B&O tax alerts that fire on `today`."""
    alerts: list[Alert] = []
    for builder in (_sparkry_monthly_alert, _blackline_quarterly_alert):
        alert = builder(today)
        if alert is not None:
            alerts.append(alert)
    return alerts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_rules.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/alerts/rules.py src/alerts/test_rules.py
git commit -m "feat(alerts): Alert dataclass + WA B&O tax rules (REQ-ALERT-001/002/003)"
```

---

## Task 4: Invoice rules

**Files:**
- Modify: `src/alerts/rules.py` (add invoice URL constants + `compute_invoice_alerts`)
- Test: `src/alerts/test_rules.py` (add invoice tests)

- [ ] **Step 1: Write the failing tests**

Append to `src/alerts/test_rules.py`:

```python
# --- Invoice rules ---------------------------------------------------------

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from src.alerts.rules import compute_invoice_alerts  # noqa: E402
from src.models.base import Base  # noqa: E402
from src.models.enums import BillingModel  # noqa: E402
from src.models.invoice import Customer, Invoice  # noqa: E402

_INV_ENGINE = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}
)
Base.metadata.create_all(_INV_ENGINE)
_InvSession = sessionmaker(bind=_INV_ENGINE)


@pytest.fixture()
def inv_session() -> Generator[Session, None, None]:
    s = _InvSession()
    yield s
    # Clean rows between tests for isolation.
    s.query(Invoice).delete()
    s.query(Customer).delete()
    s.commit()
    s.close()


def _make_customer(s: Session) -> str:
    # Customer has no `entity` column; billing_model is required.
    c = Customer(name="Cardinal Health", billing_model=BillingModel.FLAT_RATE.value)
    s.add(c)
    s.commit()
    return c.id


def _make_draft(s: Session, *, number: str, due: str | None, submitted: str | None) -> str:
    cid = _make_customer(s)
    inv = Invoice(
        invoice_number=number,
        customer_id=cid,
        entity="sparkry",
        status="draft",
        due_date=due,
        submitted_date=submitted,
        subtotal=0,
        total=100,
    )
    s.add(inv)
    s.commit()
    return inv.id


def test_sweep_fires_on_last_day_of_month(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 30), inv_session)
    keys = {a.alert_key for a in alerts}
    assert "invoice:sweep:2026-06" in keys


def test_sweep_silent_on_non_last_day(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 6, 29), inv_session)
    keys = {a.alert_key for a in alerts}
    assert "invoice:sweep:2026-06" not in keys


def test_sweep_handles_february(inv_session: Session) -> None:
    alerts = compute_invoice_alerts(date(2026, 2, 28), inv_session)
    assert "invoice:sweep:2026-02" in {a.alert_key for a in alerts}


def test_draft_invoice_fires_daily_from_due_date(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-001", due="2026-06-05", submitted=None)
    # On due date.
    a1 = compute_invoice_alerts(date(2026, 6, 5), inv_session)
    # A few days later — still draft, still fires.
    a2 = compute_invoice_alerts(date(2026, 6, 9), inv_session)
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in a1}
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in a2}


def test_draft_invoice_silent_before_due_date(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-002", due="2026-06-20", submitted=None)
    alerts = compute_invoice_alerts(date(2026, 6, 1), inv_session)
    assert f"invoice:draft:{inv_id}" not in {a.alert_key for a in alerts}


def test_non_draft_invoice_never_fires(inv_session: Session) -> None:
    cid = _make_customer(inv_session)
    inv = Invoice(
        invoice_number="202606-003",
        customer_id=cid,
        entity="sparkry",
        status="sent",
        due_date="2026-06-01",
        subtotal=0,
        total=100,
    )
    inv_session.add(inv)
    inv_session.commit()
    alerts = compute_invoice_alerts(date(2026, 6, 10), inv_session)
    assert f"invoice:draft:{inv.id}" not in {a.alert_key for a in alerts}


def test_draft_falls_back_to_submitted_date_when_due_missing(inv_session: Session) -> None:
    inv_id = _make_draft(inv_session, number="202606-004", due=None, submitted="2026-06-03")
    alerts = compute_invoice_alerts(date(2026, 6, 4), inv_session)
    assert f"invoice:draft:{inv_id}" in {a.alert_key for a in alerts}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_invoice_alerts'`

- [ ] **Step 3: Implement invoice rules**

Add to the top of `src/alerts/rules.py` (after the existing imports):

```python
from sqlalchemy.orm import Session

from src.models.invoice import Invoice
```

Add these constants near the other module constants:

```python
INVOICE_SWEEP_URL = "https://macbook.ancon-cliff.ts.net/invoices"
INVOICE_DETAIL_URL = "https://macbook.ancon-cliff.ts.net/invoices/{invoice_id}"

RECURRING_BILLERS = (
    "Cardinal Health (SAP flat-rate, monthly)",
    "How To Fascinate (calendar-based, hourly)",
)
```

Append these functions to `src/alerts/rules.py`:

```python
def _invoice_sweep_alert(today: date) -> Alert | None:
    if today.day != _last_day(today.year, today.month):
        return None
    month_key = today.strftime("%Y-%m")
    month_label = today.strftime("%B %Y")
    checklist = "\n".join(f"  - {b}" for b in RECURRING_BILLERS)
    body = (
        f"It's the last day of {month_label} — time to create & submit this "
        f"month's invoices.\n\nRecurring billers to check:\n{checklist}\n\n"
        f"Open invoicing: {INVOICE_SWEEP_URL}"
    )
    return Alert(
        alert_key=f"invoice:sweep:{month_key}",
        occurrence_date=today.isoformat(),
        alert_type="invoice_sweep",
        entity="all",
        subject=f"Submit {month_label} invoices",
        body_text=body,
        due_date=today.isoformat(),
        action_url=INVOICE_SWEEP_URL,
    )


def _draft_reminder_date(invoice: Invoice) -> str | None:
    return invoice.due_date or invoice.submitted_date or invoice.service_period_end


def _invoice_draft_alerts(today: date, session: Session) -> list[Alert]:
    drafts = session.query(Invoice).filter(Invoice.status == "draft").all()
    out: list[Alert] = []
    today_iso = today.isoformat()
    for inv in drafts:
        reminder_date = _draft_reminder_date(inv)
        if reminder_date is None or today_iso < reminder_date:
            continue
        action_url = INVOICE_DETAIL_URL.format(invoice_id=inv.id)
        body = (
            f"Draft invoice {inv.invoice_number} ({inv.entity}) for "
            f"${inv.total} has not been sent.\n"
            f"Scheduled date: {reminder_date}.\n"
            f"Open it: {action_url}"
        )
        out.append(
            Alert(
                alert_key=f"invoice:draft:{inv.id}",
                occurrence_date=today_iso,
                alert_type="invoice_draft",
                entity=inv.entity,
                subject=f"Draft invoice {inv.invoice_number} still needs to be sent",
                body_text=body,
                due_date=reminder_date,
                action_url=action_url,
            )
        )
    return out


def compute_invoice_alerts(today: date, session: Session) -> list[Alert]:
    """Return all invoice-submission alerts that fire on `today`."""
    alerts: list[Alert] = []
    sweep = _invoice_sweep_alert(today)
    if sweep is not None:
        alerts.append(sweep)
    alerts.extend(_invoice_draft_alerts(today, session))
    return alerts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_rules.py -v`
Expected: PASS (all rule tests pass)

- [ ] **Step 5: Commit**

```bash
git add src/alerts/rules.py src/alerts/test_rules.py
git commit -m "feat(alerts): invoice sweep + daily draft reminders (REQ-ALERT-004/005)"
```

> **Plan-time confirm:** the `INVOICE_SWEEP_URL` / `INVOICE_DETAIL_URL` hosts use the
> Tailscale dashboard URL. If the Hetzner deployment serves the dashboard at a different
> host, update both constants before the `--apply` cutover (spec §11 item 2).

---

## Task 5: Webhook client

**Files:**
- Create: `src/alerts/webhook.py`
- Test: `src/alerts/test_webhook.py`

- [ ] **Step 1: Write the failing test**

Create `src/alerts/test_webhook.py`:

```python
"""Tests for the n8n webhook client.

REQ-ID: REQ-ALERT-007 (DRY-RUN default — no network without apply)
REQ-ID: REQ-ALERT-009 (payload shape + secret header)
"""

import httpx
import pytest

from src.alerts.rules import Alert
from src.alerts.webhook import WebhookResult, build_payload, post_alert

_ALERT = Alert(
    alert_key="tax:sparkry:bo:2026-04",
    occurrence_date="2026-05-10",
    alert_type="tax_bo",
    entity="sparkry",
    subject="WA B&O due — Sparkry LLC",
    body_text="body here",
    due_date="2026-05-25",
    action_url="https://secure.dor.wa.gov/home/Login",
)


def test_build_payload_has_required_fields() -> None:
    payload = build_payload(_ALERT, "Travis@sparkry.com", "ea-alerts@sparkry.com")
    assert payload["from"] == "Travis@sparkry.com"
    assert payload["to"] == "ea-alerts@sparkry.com"
    assert payload["subject"] == "WA B&O due — Sparkry LLC"
    assert payload["body_text"] == "body here"
    assert payload["alert_type"] == "tax_bo"
    assert payload["entity"] == "sparkry"
    assert payload["due_date"] == "2026-05-25"
    assert payload["action_url"] == "https://secure.dor.wa.gov/home/Login"
    assert payload["alert_key"] == "tax:sparkry:bo:2026-04"
    assert payload["occurrence_date"] == "2026-05-10"


def test_dry_run_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("network call made during DRY-RUN")

    monkeypatch.setattr(httpx, "post", _boom)
    result = post_alert(_ALERT, apply=False)
    assert isinstance(result, WebhookResult)
    assert result.status == "dry_run"
    assert result.http_status is None


def test_apply_posts_with_secret_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class _Resp:
        status_code = 200
        is_success = True
        text = "ok"

    def _fake_post(url, json, headers, timeout):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_URL", "https://n8n.example/webhook/alerts")
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "post", _fake_post)

    result = post_alert(_ALERT, apply=True)
    assert result.status == "sent"
    assert result.http_status == 200
    assert captured["url"] == "https://n8n.example/webhook/alerts"
    assert captured["headers"]["X-Webhook-Secret"] == "s3cret"
    assert captured["json"]["alert_key"] == "tax:sparkry:bo:2026-04"


def test_apply_without_config_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("N8N_ALERTS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("N8N_ALERTS_WEBHOOK_SECRET", raising=False)
    result = post_alert(_ALERT, apply=True)
    assert result.status == "failed"
    assert result.http_status is None


def test_apply_non_2xx_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 500
        is_success = False
        text = "boom"

    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_URL", "https://n8n.example/webhook/alerts")
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    result = post_alert(_ALERT, apply=True)
    assert result.status == "failed"
    assert result.http_status == 500
```

- [ ] **Step 2: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.alerts.webhook'`

- [ ] **Step 3: Implement the webhook client**

Create `src/alerts/webhook.py`:

```python
"""n8n webhook client for EA alerts.

DRY-RUN by default: `post_alert(alert, apply=False)` builds the payload but makes
no network call.  Only `apply=True` POSTs to the configured n8n webhook.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from src.alerts.rules import Alert

logger = logging.getLogger(__name__)

DEFAULT_FROM = "Travis@sparkry.com"
DEFAULT_TO = "ea-alerts@sparkry.com"


@dataclass(frozen=True)
class WebhookResult:
    status: str  # "sent" | "failed" | "dry_run"
    http_status: int | None
    error: str | None


def build_payload(alert: Alert, from_email: str, to_email: str) -> dict:
    return {
        "from": from_email,
        "to": to_email,
        "subject": alert.subject,
        "body_text": alert.body_text,
        "body_html": alert.body_html,
        "alert_type": alert.alert_type,
        "entity": alert.entity,
        "due_date": alert.due_date,
        "action_url": alert.action_url,
        "alert_key": alert.alert_key,
        "occurrence_date": alert.occurrence_date,
    }


def post_alert(alert: Alert, *, apply: bool, timeout: float = 10.0) -> WebhookResult:
    from_email = os.environ.get("ALERT_FROM_EMAIL", DEFAULT_FROM)
    to_email = os.environ.get("ALERT_TO_EMAIL", DEFAULT_TO)
    payload = build_payload(alert, from_email, to_email)

    if not apply:
        logger.info("DRY-RUN alert %s (%s)", alert.alert_key, alert.subject)
        return WebhookResult("dry_run", None, None)

    url = os.environ.get("N8N_ALERTS_WEBHOOK_URL", "")
    secret = os.environ.get("N8N_ALERTS_WEBHOOK_SECRET", "")
    if not url or not secret:
        return WebhookResult(
            "failed", None, "N8N_ALERTS_WEBHOOK_URL/SECRET not configured"
        )

    headers = {"X-Webhook-Secret": secret, "Content-Type": "application/json"}
    # SECURITY: never log the secret value.
    logger.debug("POST n8n alerts webhook key=%s", alert.alert_key)
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return WebhookResult("failed", None, str(exc))

    if resp.is_success:
        return WebhookResult("sent", resp.status_code, None)
    return WebhookResult("failed", resp.status_code, resp.text[:500])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_webhook.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/alerts/webhook.py src/alerts/test_webhook.py
git commit -m "feat(alerts): n8n webhook client, DRY-RUN default (REQ-ALERT-007/009)"
```

---

## Task 6: Dispatcher (dedup + per-alert isolation)

**Files:**
- Create: `src/alerts/dispatcher.py`
- Test: `src/alerts/test_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Create `src/alerts/test_dispatcher.py`:

```python
"""Tests for the alert dispatcher.

REQ-ID: REQ-ALERT-006 (dedup — running twice sends once)
REQ-ID: REQ-ALERT-008 (per-alert error isolation)
"""

from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.alerts.dispatcher as dispatcher_mod
from src.alerts.dispatcher import dispatch_alerts
from src.alerts.models import AlertDispatch
from src.alerts.rules import Alert
from src.alerts.webhook import WebhookResult
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(AlertDispatch).delete()
    s.commit()
    s.close()


def _alert(key: str) -> Alert:
    return Alert(
        alert_key=key,
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="subj",
        body_text="body",
        due_date="2026-05-25",
        action_url="https://secure.dor.wa.gov/home/Login",
    )


def test_apply_records_sent_and_dedupes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(
        dispatcher_mod, "compute_invoice_alerts", lambda today, s: []
    )
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )

    s1 = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert s1.sent == 1
    # Second run on the same day must dedupe.
    s2 = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert s2.sent == 0
    assert s2.skipped == 1
    rows = session.query(AlertDispatch).filter_by(alert_key="k1").all()
    assert len(rows) == 1


def test_failed_alert_does_not_block_others_and_retries(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: [_alert("bad"), _alert("good")],
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    def _post(a: Alert, apply: bool) -> WebhookResult:
        if a.alert_key == "bad":
            return WebhookResult("failed", 500, "boom")
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_alert", _post)

    summary = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert summary.sent == 1
    assert summary.failed == 1
    # The failed one is NOT marked sent, so it retries next run.
    bad = session.query(AlertDispatch).filter_by(alert_key="bad").one()
    assert bad.status == "failed"


def test_dry_run_writes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod,
        "post_alert",
        lambda a, apply: WebhookResult("dry_run", None, None),
    )
    summary = dispatch_alerts(session, date(2026, 5, 10), apply=False)
    assert summary.dry_run == 1
    assert session.query(AlertDispatch).count() == 0


def test_exception_in_post_is_isolated(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: [_alert("boom"), _alert("ok")],
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    def _post(a: Alert, apply: bool) -> WebhookResult:
        if a.alert_key == "boom":
            raise RuntimeError("kaboom")
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_alert", _post)
    summary = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert summary.sent == 1
    assert summary.failed == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_dispatcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.alerts.dispatcher'`

- [ ] **Step 3: Implement the dispatcher**

Create `src/alerts/dispatcher.py`:

```python
"""Alert dispatcher: compute → dedupe → POST → record.

Per-alert error isolation: one failed/raising alert never blocks the rest, and
an alert is only skipped on later runs if it was previously recorded as "sent".
DRY-RUN (apply=False) writes nothing to the ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.rules import Alert, compute_invoice_alerts, compute_tax_alerts
from src.alerts.webhook import WebhookResult, post_alert

logger = logging.getLogger(__name__)


@dataclass
class DispatchSummary:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: int = 0


def _already_sent(session: Session, alert: Alert) -> AlertDispatch | None:
    return (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert.alert_key, occurrence_date=alert.occurrence_date)
        .one_or_none()
    )


def _record(session: Session, alert: Alert, result: WebhookResult) -> None:
    existing = _already_sent(session, alert)
    row = existing or AlertDispatch(
        alert_key=alert.alert_key,
        occurrence_date=alert.occurrence_date,
        alert_type=alert.alert_type,
        entity=alert.entity,
        subject=alert.subject,
        status=result.status,
    )
    row.status = result.status
    row.http_status = result.http_status
    row.error_detail = result.error
    if existing is None:
        session.add(row)
    session.commit()


def dispatch_alerts(session: Session, today: date, *, apply: bool) -> DispatchSummary:
    alerts = compute_tax_alerts(today) + compute_invoice_alerts(today, session)
    summary = DispatchSummary()

    for alert in alerts:
        if apply:
            existing = _already_sent(session, alert)
            if existing is not None and existing.status == "sent":
                summary.skipped += 1
                continue

        try:
            result = post_alert(alert, apply=apply)
        except Exception as exc:  # noqa: BLE001 — isolate one bad alert
            logger.exception("alert %s raised during dispatch", alert.alert_key)
            result = WebhookResult("failed", None, str(exc))

        if result.status == "sent":
            summary.sent += 1
        elif result.status == "dry_run":
            summary.dry_run += 1
        else:
            summary.failed += 1

        if apply:
            try:
                _record(session, alert, result)
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("failed to record alert %s", alert.alert_key)

    return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_dispatcher.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/alerts/dispatcher.py src/alerts/test_dispatcher.py
git commit -m "feat(alerts): dispatcher with dedup + per-alert isolation (REQ-ALERT-006/008)"
```

---

## Task 7: CLI entrypoint

**Files:**
- Create: `scripts/alerts_dispatch.py`
- Test: `src/alerts/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `src/alerts/test_cli.py`:

```python
"""Tests for the alerts_dispatch CLI.

REQ-ID: REQ-ALERT-007 (DRY-RUN is the default; --apply opts in)
"""

import runpy
import sys
from datetime import date

import pytest


def _run(argv: list[str]) -> None:
    old = sys.argv
    sys.argv = ["alerts_dispatch.py", *argv]
    try:
        runpy.run_module("scripts.alerts_dispatch", run_name="__main__")
    finally:
        sys.argv = old


def test_default_is_dry_run(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    captured = {}

    import scripts.alerts_dispatch as cli

    def _fake_dispatch(session, today, *, apply):
        captured["apply"] = apply
        captured["today"] = today
        from src.alerts.dispatcher import DispatchSummary

        return DispatchSummary(dry_run=2)

    monkeypatch.setattr(cli, "dispatch_alerts", _fake_dispatch)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())

    _run(["--date", "2026-05-10"])
    assert captured["apply"] is False
    assert captured["today"] == date(2026, 5, 10)


def test_apply_flag_enables_send(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    import scripts.alerts_dispatch as cli

    def _fake_dispatch(session, today, *, apply):
        captured["apply"] = apply
        from src.alerts.dispatcher import DispatchSummary

        return DispatchSummary(sent=1)

    monkeypatch.setattr(cli, "dispatch_alerts", _fake_dispatch)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _DummySession())

    _run(["--apply", "--date", "2026-05-10"])
    assert captured["apply"] is True


class _DummySession:
    def close(self) -> None:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `doppler run -- pytest src/alerts/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.alerts_dispatch'`

- [ ] **Step 3: Implement the CLI**

Create `scripts/alerts_dispatch.py`:

```python
#!/usr/bin/env python3
"""Daily EA alert dispatch.

DRY-RUN by default — prints what *would* send and makes no network call.
Pass --apply to POST to the n8n webhook and record sends in alert_dispatch.

Invoked by com.sparkry.alerts-dispatch.timer (systemd) on the Hetzner box.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.alerts.dispatcher import dispatch_alerts  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dispatch EA alerts to the n8n webhook.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually POST to n8n and record sends (default: DRY-RUN).",
    )
    p.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Override today's date (YYYY-MM-DD) for testing.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    today = args.date or date.today()
    init_db()
    session = SessionLocal()
    try:
        summary = dispatch_alerts(session, today, apply=args.apply)
    finally:
        session.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] {today.isoformat()} — "
        f"sent={summary.sent} skipped={summary.skipped} "
        f"failed={summary.failed} dry_run={summary.dry_run}"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `doppler run -- pytest src/alerts/test_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Smoke-test the real DRY-RUN against the live DB**

Run: `doppler run -- python scripts/alerts_dispatch.py --date 2026-06-30`
Expected: prints `[DRY-RUN] 2026-06-30 — sent=0 skipped=0 failed=0 dry_run=N` (N ≥ 1 because the 30th is a sweep day). No network call, no DB writes.

- [ ] **Step 6: Commit**

```bash
git add scripts/alerts_dispatch.py src/alerts/test_cli.py
git commit -m "feat(alerts): alerts_dispatch CLI, DRY-RUN default (REQ-ALERT-007)"
```

---

## Task 8: Scheduling units (systemd + plist parity)

**Files:**
- Create: `deploy/com.sparkry.alerts-dispatch.service`
- Create: `deploy/com.sparkry.alerts-dispatch.timer`
- Create: `com.sparkry.alerts-dispatch.plist`

> No automated test — these are deployment artifacts. Validation is the DRY-RUN smoke
> from Task 7 plus the manual `systemd-analyze verify` below.

- [ ] **Step 1: Create the systemd service unit**

Create `deploy/com.sparkry.alerts-dispatch.service`:

```ini
[Unit]
Description=Sparkry accounting — daily EA alert dispatch
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/accounting
ExecStart=/usr/bin/doppler run --project accounting --config dev -- \
    /opt/accounting/.venv/bin/python scripts/alerts_dispatch.py --apply
# Until N8N_ALERTS_WEBHOOK_URL/SECRET are provisioned, drop --apply to stay DRY-RUN.
```

> Adjust `WorkingDirectory`, the venv path, and `--config` to match the Hetzner box.
> Keep `--apply` off until the Doppler webhook keys exist (the job runs DRY-RUN safely).

- [ ] **Step 2: Create the systemd timer unit**

Create `deploy/com.sparkry.alerts-dispatch.timer`:

```ini
[Unit]
Description=Run Sparkry EA alert dispatch daily at 07:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Create the macOS LaunchAgent parity plist**

Create `com.sparkry.alerts-dispatch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sparkry.alerts-dispatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/doppler</string>
        <string>run</string>
        <string>--project</string><string>accounting</string>
        <string>--config</string><string>dev</string>
        <string>--</string>
        <string>/Users/travis/SGDrive/dev/accounting/.venv/bin/python</string>
        <string>/Users/travis/SGDrive/dev/accounting/scripts/alerts_dispatch.py</string>
        <string>--apply</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/travis/SGDrive/dev/accounting/logs/alerts-dispatch.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/travis/SGDrive/dev/accounting/logs/alerts-dispatch.err</string>
</dict>
</plist>
```

- [ ] **Step 4: Verify the systemd unit syntax (on the Hetzner box, or skip locally)**

Run (on Hetzner): `systemd-analyze verify deploy/com.sparkry.alerts-dispatch.service`
Expected: no output (valid). Locally on macOS this step is skipped.

- [ ] **Step 5: Commit**

```bash
git add deploy/com.sparkry.alerts-dispatch.service deploy/com.sparkry.alerts-dispatch.timer com.sparkry.alerts-dispatch.plist
git commit -m "feat(alerts): daily dispatch scheduling units (systemd + plist parity)"
```

---

## Task 9: Final quality gates + docs

**Files:**
- Modify: `CLAUDE.md` (add the alerts dispatch command + service row)
- Modify: `requirements/current.md` (add REQ-ALERT-001..010)

- [ ] **Step 1: Run the full quality gates**

Run:
```bash
doppler run -- pytest src/alerts/ -v
doppler run -- ruff check src/alerts/ scripts/alerts_dispatch.py
doppler run -- mypy src/alerts/
```
Expected: all green. Fix any failures before continuing.

- [ ] **Step 2: Add REQ-IDs to requirements**

In `requirements/current.md`, add a section:

```markdown
### EA Alert Routing (REQ-ALERT-001..010)

- REQ-ALERT-001 — Sparkry monthly WA B&O reminders fire on the 3rd/10th/17th/25th.
- REQ-ALERT-002 — BlackLine quarterly WA B&O reminders fire weekly through the due date.
- REQ-ALERT-003 — tax_bo email carries DOR account ID, filing period, due date, login URL.
- REQ-ALERT-004 — Invoice sweep fires once on the last calendar day of the month.
- REQ-ALERT-005 — Draft invoices remind daily from their date until status leaves draft.
- REQ-ALERT-006 — Dedup: one send per (alert_key, occurrence_date).
- REQ-ALERT-007 — DRY-RUN is the default; --apply opts into sending.
- REQ-ALERT-008 — Per-alert error isolation; failed alerts retry next run.
- REQ-ALERT-009 — Webhook POST sends the documented payload + X-Webhook-Secret header.
- REQ-ALERT-010 — alert_dispatch migration is additive with a clean downgrade.
```

- [ ] **Step 3: Add the command + service to CLAUDE.md**

In `CLAUDE.md` under Development Commands, add:

```bash
# EA alert dispatch (DRY-RUN default; --apply to POST to n8n)
doppler run -- python scripts/alerts_dispatch.py            # dry-run, today
doppler run -- python scripts/alerts_dispatch.py --apply    # send
doppler run -- python scripts/alerts_dispatch.py --date 2026-06-30  # test a date
```

And add a row to the Local Deployment table:

```markdown
| Alerts Dispatch | `com.sparkry.alerts-dispatch.{service,timer}` | — | Daily EA tax + invoice alert emails via n8n webhook (`scripts/alerts_dispatch.py --apply`). DRY-RUN until `N8N_ALERTS_WEBHOOK_URL/SECRET` provisioned. |
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md requirements/current.md
git commit -m "docs(alerts): document dispatch command + REQ-ALERT-001..010"
```

---

## Self-Review Notes (addressed)

- **Spec coverage:** §2 push model → Tasks 5–8; §4.1 tax → Task 3; §4.2/4.3 invoice → Task 4; §5 ledger → Tasks 1–2; §6 webhook contract → Task 5 (`build_payload`); §7 config → Task 5 env reads + Task 8 units; §8 scheduling → Task 8; §9 tests → REQ-ALERT-001..010 across tasks.
- **Open items (spec §11):** webhook keys provisioned later — DRY-RUN is safe until then (Task 8 note); dashboard URLs flagged for confirm (Task 4 note); DRY-RUN writes nothing (Task 6 `test_dry_run_writes_nothing`).
- **Type consistency:** `Alert`, `WebhookResult`, `DispatchSummary`, `dispatch_alerts(session, today, *, apply)`, `post_alert(alert, *, apply)`, `compute_tax_alerts(today)`, `compute_invoice_alerts(today, session)` used identically across all tasks.
- **Out of scope (spec §10):** no "mark B&O filed" ack, no customer payment dunning, single recipient.
