"""Engine unit tests + source-spec §7 regression."""
from __future__ import annotations

import pytest

from src.planning.engine import (
    loan_payment,
    real_spend,
)
from src.planning.params import DEFAULTS


def test_real_spend_flat_through_age_69() -> None:
    """While age < glide_start_age, spend stays at spend_start."""
    for age in range(49, 70):
        assert real_spend(age, DEFAULTS) == DEFAULTS.spend_start


def test_real_spend_glides_toward_floor_at_70_plus() -> None:
    """At/after glide_start_age, spend decays geometrically toward spend_floor."""
    s70 = real_spend(70, DEFAULTS)
    s75 = real_spend(75, DEFAULTS)
    s90 = real_spend(90, DEFAULTS)
    # Per source-spec §4 expected real path
    assert 240_000 < s70 < 250_000  # ~$240k at 70 (close to start)
    assert 195_000 < s75 < 210_000  # ~$201k at 75
    assert s90 < s75 < s70          # monotonic decrease
    assert s90 > DEFAULTS.spend_floor  # never below floor


def test_loan_payment_modes() -> None:
    """Three loan modes per source spec."""
    import dataclasses
    base = dataclasses.replace(DEFAULTS, loan=1_000_000.0, loan_rate=0.0675)

    p_none = dataclasses.replace(base, loan_mode="none")
    assert loan_payment(p_none) == 0.0

    p_io = dataclasses.replace(base, loan_mode="interest_only")
    assert loan_payment(p_io) == pytest.approx(67_500.0)

    p_am = dataclasses.replace(base, loan_mode="amortize10")
    # 10-yr amortizing at 6.75% on $1M ≈ $141,478/yr
    assert 140_000 < loan_payment(p_am) < 143_000
