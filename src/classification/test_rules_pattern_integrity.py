"""Tests for vendor-rule pattern integrity — the dead-rule regression.

Incident 2026-09-02: 34 of 76 production vendor rules carried regex patterns
(`cardinal.*health|fascinate.*os`, `\\bshopify\\b`, `amazon.*aws|aws\\.amazon`)
stored with ``is_regex=False``. REQ-FIX-ING-005 made ``is_regex=False`` mean
``re.escape``-ed literal matching and defaulted every pre-migration row to
False, so those rules silently stopped matching anything. Tier 1 fell through
to Tier 3 for half the vendor book; Gemini then classified two $31,000
Cardinal Health consulting payments as personal MEDICAL expenses.

REQ-ID: REQ-FIX-ING-022  A pattern carrying hard regex metacharacters is never
                          storable as a literal: ``validate_pattern_flag``
                          rejects the combination, and every rule-creation site
                          enforces it.
REQ-ID: REQ-FIX-ING-023  ``repair_literal_regex_rules`` finds every existing
                          is_regex=False rule whose pattern holds hard
                          metacharacters and compiles cleanly, flips it to
                          is_regex=True, and is idempotent + DRY-RUN default.
REQ-ID: REQ-FIX-ING-024  ``normalize_learned_pattern`` strips per-payment ACH
                          noise (TRACE#, TRN:, EED:, ORIG ID:, RMR* remittance,
                          amounts) so a learned rule generalizes to the next
                          payment from the same originator instead of matching
                          exactly one transaction forever.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.classification.rules import (
    ENTITY_TYPE_VENDOR_RULE,
    PatternFlagError,
    lookup_vendor_rule,
    make_learned_vendor_rule,
    normalize_learned_pattern,
    repair_literal_regex_rules,
    validate_pattern_flag,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, VendorRuleSource
from src.models.vendor_rule import VendorRule

# The exact production strings from the incident.
CARDINAL_RULE = r"cardinal.*health|fascinate.*os"
CARDINAL_AUG = (
    "ORIG CO NAME:CARDINAL HEALTH, ORIG ID:1310958666 DESC DATE: CO ENTRY "
    "DESCR:EFTPAYMENTSEC:CCD TRACE#:091000019854840 EED:260831 IND ID: IND "
    "NAME:SPARKRY LLC RMR*OI*CH20260430**31000.00*31000.00*0.00\\ DIRECT "
    "DEPOSIT TRN: 2439854840TC"
)
CARDINAL_JUL = (
    "ORIG CO NAME:CARDINAL HEALTH, ORIG ID:1310958666 DESC DATE: CO ENTRY "
    "DESCR:EFTPAYMENTSEC:CCD TRACE#:091000012243053 EED:260706 IND ID: IND "
    "NAME:SPARKRY LLC RMR*OI*CH20260331**33000.00*33000.00*0.00\\ DIRECT "
    "DEPOSIT TRN: 1872243053TC"
)
# REQ-FIX-ING-024 test case: originator with DESC DATE populated before TRACE#.
# If DESC DATE is per-payment (P2-b8c), the learned rule must generalize across
# different DESC DATE values. Currently the head includes DESC DATE, so the
# pattern is stable across payments if DESC DATE is always empty; adding this
# fixture pins the behavior when DESC DATE has a value.
CARDINAL_WITH_POPULATED_DESC_DATE = (
    "ORIG CO NAME:CARDINAL HEALTH, ORIG ID:1310958666 DESC DATE:20260829 "
    "CO ENTRY DESCR:EFTPAYMENTSEC:CCD TRACE#:091000019854840 EED:260831 "
    "IND ID: IND NAME:SPARKRY LLC RMR*OI*CH20260430**31000.00*31000.00*0.00\\ "
    "DIRECT DEPOSIT TRN: 2439854840TC"
)


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


def _rule(
    session: Session,
    pattern: str,
    *,
    is_regex: bool = False,
    entity: Entity = Entity.SPARKRY,
    tax_category: TaxCategory = TaxCategory.SUPPLIES,
    direction: Direction = Direction.EXPENSE,
    examples: int = 1,
    confidence: float = 0.90,
) -> VendorRule:
    rule = VendorRule(
        vendor_pattern=pattern,
        is_regex=is_regex,
        entity=entity.value,
        tax_category=tax_category.value,
        direction=direction.value,
        examples=examples,
        confidence=confidence,
    )
    session.add(rule)
    session.commit()
    return rule


# ---------------------------------------------------------------------------
# REQ-FIX-ING-022: a metacharacter pattern is never storable as a literal
# ---------------------------------------------------------------------------


class TestValidatePatternFlag:
    @pytest.mark.parametrize(
        "pattern",
        [
            CARDINAL_RULE,
            r"\bshopify\b",
            r"amazon.*aws|aws\.amazon",
            r"\bfedex\b",
            r"stripe.*substack|substack.*stripe",
            r"google.*workspace|google.*payments",
        ],
    )
    def test_hard_metacharacter_pattern_rejected_as_literal(
        self, pattern: str
    ) -> None:
        """The six real production patterns cannot be saved is_regex=False."""
        with pytest.raises(PatternFlagError):
            validate_pattern_flag(pattern, is_regex=False)

    @pytest.mark.parametrize(
        "pattern",
        [CARDINAL_RULE, r"\bshopify\b", r"amazon.*aws|aws\.amazon"],
    )
    def test_same_pattern_accepted_as_regex(self, pattern: str) -> None:
        validate_pattern_flag(pattern, is_regex=True)  # must not raise

    @pytest.mark.parametrize(
        "pattern",
        [
            "Anthropic Headquarters",
            "SpringHill Suites by Marriott",
            "amazon.com",  # a bare dot still matches itself literally
            "Cardinal Health East Cafe",
            # Real descriptors that carry punctuation and must NOT be flagged.
            # Square and PayPal put a literal '*' in every descriptor, and
            # "A.B Corp (West)" is the case src/api/test_vendor_rules.py
            # already protects.
            "SQ *COFFEE SHOP",
            "PAYPAL *VENDOR",
            "A.B Corp (West)",
            "AT&T*BILL PAYMENT",
            "Smith + Sons",
            "amazon(unclosed",
        ],
    )
    def test_plain_vendor_string_still_allowed_as_literal(
        self, pattern: str
    ) -> None:
        validate_pattern_flag(pattern, is_regex=False)  # must not raise

    @pytest.mark.parametrize(
        "pattern", ["SQ *COFFEE SHOP", "A.B Corp (West)", "amazon(unclosed"]
    )
    def test_repair_never_touches_a_punctuated_literal(
        self, session: Session, pattern: str
    ) -> None:
        """Regression guard: the repair must not convert a real descriptor
        into a regex just because it contains punctuation."""
        rule = _rule(session, pattern, is_regex=False)
        result = repair_literal_regex_rules(session, dry_run=False)
        assert result.repaired == 0
        session.refresh(rule)
        assert rule.is_regex is False

    def test_invalid_regex_rejected_even_when_flagged_regex(self) -> None:
        with pytest.raises(PatternFlagError):
            validate_pattern_flag(r"cardinal(health", is_regex=True)

    def test_empty_pattern_rejected(self) -> None:
        with pytest.raises(PatternFlagError):
            validate_pattern_flag("", is_regex=False)


# ---------------------------------------------------------------------------
# REQ-FIX-ING-023: repair the rules already poisoned in production
# ---------------------------------------------------------------------------


class TestRepairLiteralRegexRules:
    def test_dry_run_default_reports_without_writing(
        self, session: Session
    ) -> None:
        rule = _rule(session, CARDINAL_RULE, is_regex=False)
        result = repair_literal_regex_rules(session)
        assert result.repaired == 1
        assert result.dry_run is True
        session.refresh(rule)
        assert rule.is_regex is False, "dry run must not mutate"

    def test_apply_flips_flag_and_rule_matches_again(
        self, session: Session
    ) -> None:
        _rule(
            session,
            CARDINAL_RULE,
            is_regex=False,
            tax_category=TaxCategory.CONSULTING_INCOME,
            direction=Direction.INCOME,
            confidence=0.97,
        )
        assert lookup_vendor_rule(CARDINAL_AUG, session) is None

        result = repair_literal_regex_rules(session, dry_run=False)
        assert result.repaired == 1

        hit = lookup_vendor_rule(CARDINAL_AUG, session)
        assert hit is not None, "repaired rule must match the Aug 29 payment"
        assert hit.tax_category is TaxCategory.CONSULTING_INCOME
        assert hit.direction is Direction.INCOME

    def test_leaves_plain_literal_rules_untouched(self, session: Session) -> None:
        plain = _rule(session, "Anthropic Headquarters", is_regex=False)
        result = repair_literal_regex_rules(session, dry_run=False)
        assert result.repaired == 0
        session.refresh(plain)
        assert plain.is_regex is False

    def test_skips_uncompilable_pattern_and_reports_it(
        self, session: Session
    ) -> None:
        """Reads as a regex (dot-star + alternation) but will not compile
        (unclosed group), so a human must decide what it meant."""
        broken = _rule(session, r"cardinal.*health|fascinate(os", is_regex=False)
        result = repair_literal_regex_rules(session, dry_run=False)
        assert result.repaired == 0
        assert result.skipped == 1
        session.refresh(broken)
        assert broken.is_regex is False, "never flip a pattern that cannot compile"

    def test_is_idempotent(self, session: Session) -> None:
        _rule(session, CARDINAL_RULE, is_regex=False)
        first = repair_literal_regex_rules(session, dry_run=False)
        second = repair_literal_regex_rules(session, dry_run=False)
        assert first.repaired == 1
        assert second.repaired == 0


# ---------------------------------------------------------------------------
# REQ-FIX-ING-024: learned patterns must generalize past one payment
# ---------------------------------------------------------------------------


class TestNormalizeLearnedPattern:
    def test_ach_noise_stripped_so_next_payment_matches(self) -> None:
        learned = normalize_learned_pattern(CARDINAL_JUL)
        assert "TRACE#" not in learned
        assert "091000012243053" not in learned
        assert "260706" not in learned
        assert "33000.00" not in learned
        assert "CH20260331" not in learned
        assert "CARDINAL HEALTH" in learned.upper()

    def test_two_payments_from_same_originator_normalize_identically(
        self,
    ) -> None:
        assert normalize_learned_pattern(CARDINAL_JUL) == normalize_learned_pattern(
            CARDINAL_AUG
        )

    def test_normalized_pattern_matches_a_later_payment(
        self, session: Session
    ) -> None:
        _rule(
            session,
            normalize_learned_pattern(CARDINAL_JUL),
            is_regex=False,
            tax_category=TaxCategory.CONSULTING_INCOME,
            direction=Direction.INCOME,
        )
        hit = lookup_vendor_rule(CARDINAL_AUG, session)
        assert hit is not None, "a rule learned in July must catch the Aug payment"

    def test_plain_card_descriptions_pass_through_unchanged(self) -> None:
        for desc in ("Anthropic Headquarters", "Avis", "Shopify-charge"):
            assert normalize_learned_pattern(desc) == desc

    def test_never_returns_empty_for_a_noise_only_string(self) -> None:
        noisy = "TRACE#:091000019854840 EED:260831 TRN: 2439854840TC"
        assert normalize_learned_pattern(noisy).strip() != ""

    def test_populated_desc_date_never_leaks_into_the_pattern(self) -> None:
        """REQ-FIX-ING-024 / qreview P2-b8c: when an originator populates
        DESC DATE the value is that payment's own date. A pattern carrying it
        would match exactly one transaction, which is the defect this function
        exists to remove, so DESC DATE is a per-payment marker too."""
        normalized = normalize_learned_pattern(CARDINAL_WITH_POPULATED_DESC_DATE)
        assert "20260829" not in normalized
        assert "DESC DATE" not in normalized.upper()
        assert "CARDINAL HEALTH" in normalized.upper()

    def test_all_three_cardinal_payments_normalize_identically(self) -> None:
        """Empty DESC DATE, populated DESC DATE, different trace numbers and
        different amounts must all collapse to the same originator header."""
        heads = {
            normalize_learned_pattern(CARDINAL_JUL),
            normalize_learned_pattern(CARDINAL_AUG),
            normalize_learned_pattern(CARDINAL_WITH_POPULATED_DESC_DATE),
        }
        assert len(heads) == 1, heads
        assert "ORIG ID:1310958666" in heads.pop()


# ---------------------------------------------------------------------------
# qreview round 1 findings — dedup round-trip and seed-file defense
# ---------------------------------------------------------------------------


class TestLearnedRuleDedup:
    """REQ-FIX-ING-024: normalizing on write is only half the job. Every
    learning-loop entry point must ALSO normalize the pattern it dedups on,
    or each ACH payment creates a second rule instead of incrementing the
    first — the rule-spam the normalization exists to stop.
    """

    def test_normalize_is_idempotent(self) -> None:
        once = normalize_learned_pattern(CARDINAL_AUG)
        assert normalize_learned_pattern(once) == once

    def test_exact_literal_lookup_finds_rule_stored_as_normalized_head(
        self, session: Session
    ) -> None:
        from src.classification.rules import find_exact_literal_rule

        _rule(session, normalize_learned_pattern(CARDINAL_JUL), is_regex=False)
        found = find_exact_literal_rule(session, CARDINAL_AUG, Entity.SPARKRY.value)
        assert found is not None, (
            "the August payment must dedup onto the rule learned in July"
        )


class TestSeedRulesObeyTheGuard:
    """REQ-FIX-ING-022 defense in depth: the seed file is the other place a
    human writes patterns by hand, and it is where the poisoned rules came
    from. A new seed regex without is_regex=True must fail here, not in
    production six weeks later.
    """

    def test_every_seed_rule_pattern_flag_pair_is_valid(self) -> None:
        from src.classification.seed_rules import SEED_RULES

        bad: list[str] = []
        for defn in SEED_RULES:
            try:
                validate_pattern_flag(defn.vendor_pattern, is_regex=defn.is_regex)
            except PatternFlagError as exc:
                bad.append(f"{defn.vendor_pattern!r}: {exc}")
        assert not bad, "seed rules violating REQ-FIX-ING-022:\n" + "\n".join(bad)


class TestNormalizeNeverEmitsBoilerplate:
    """qreview P1-f6a: truncating at the first ACH marker can leave nothing but
    the network's own boilerplate. A rule on that header would match EVERY ACH
    payment and misclassify the lot. When no distinctive originator token
    survives, degrade to the raw description — one-shot matching is the old
    behavior and is strictly safer than over-matching.
    """

    BOILERPLATE_ONLY = (
        "ORIG CO NAME: ORIG ID: DESC DATE: CO ENTRY DESCR:EFTPAYMENTSEC:CCD "
        "TRACE#:091000019854840 EED:260831"
    )

    def test_boilerplate_only_header_falls_back_to_the_raw_description(
        self,
    ) -> None:
        assert normalize_learned_pattern(self.BOILERPLATE_ONLY) == self.BOILERPLATE_ONLY

    def test_boilerplate_header_never_matches_an_unrelated_ach_payment(
        self, session: Session
    ) -> None:
        other = (
            "ORIG CO NAME:ACME WIDGETS, ORIG ID:9999999999 DESC DATE: CO ENTRY "
            "DESCR:EFTPAYMENTSEC:CCD TRACE#:000000000000001 EED:260901"
        )
        _rule(session, normalize_learned_pattern(self.BOILERPLATE_ONLY))
        assert lookup_vendor_rule(other, session) is None

    def test_a_named_originator_still_generalizes(self) -> None:
        """The Cardinal case must keep working — the guard only rejects heads
        with no distinctive token left after boilerplate is removed."""
        assert normalize_learned_pattern(CARDINAL_JUL) != CARDINAL_JUL
        assert normalize_learned_pattern(CARDINAL_JUL) == normalize_learned_pattern(
            CARDINAL_AUG
        )


class TestMakeLearnedVendorRule:
    """qreview P1-d4e: the "every learned rule's pattern is normalized"
    invariant was enforced by convention at three separate construction sites.
    A fourth learn-site that forgets ``normalize_learned_pattern`` reopens the
    one-shot-rule bug REQ-FIX-ING-024 closed. ``make_learned_vendor_rule`` is
    the single chokepoint every learn-site must route through, so the invariant
    is structural, not per-site discipline.
    """

    def test_pattern_is_normalized_at_the_chokepoint(self) -> None:
        rule = make_learned_vendor_rule(
            description=CARDINAL_AUG,
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.CONTRACT_LABOR.value,
            direction=Direction.INCOME.value,
        )
        assert rule.vendor_pattern == normalize_learned_pattern(CARDINAL_AUG)
        assert rule.vendor_pattern != CARDINAL_AUG  # actually truncated the head

    def test_two_payments_from_one_originator_produce_the_same_pattern(
        self,
    ) -> None:
        a = make_learned_vendor_rule(
            description=CARDINAL_AUG,
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.CONTRACT_LABOR.value,
            direction=Direction.INCOME.value,
        )
        b = make_learned_vendor_rule(
            description=CARDINAL_JUL,
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.CONTRACT_LABOR.value,
            direction=Direction.INCOME.value,
        )
        assert a.vendor_pattern == b.vendor_pattern

    def test_learned_rule_is_always_a_literal(self) -> None:
        """Learned rules are literals — a normalized head is matched via
        ``re.escape`` so any residual metacharacter is matched verbatim."""
        rule = make_learned_vendor_rule(
            description="A.B Corp (West) | East",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )
        assert rule.is_regex is False

    def test_defaults_match_the_learn_sites(self) -> None:
        """The factory must reproduce the field values the three inlined
        construction sites used, or replacing them changes behavior."""
        rule = make_learned_vendor_rule(
            description="Anthropic, PBC",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )
        assert rule.confidence == 0.80
        assert rule.source == VendorRuleSource.LEARNED.value
        assert rule.examples == 1

    def test_plain_vendor_description_passes_through(self) -> None:
        rule = make_learned_vendor_rule(
            description="Anthropic, PBC",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )
        assert rule.vendor_pattern == "Anthropic, PBC"

    def test_omitted_deductible_pct_coalesces_to_the_column_default(self) -> None:
        """The invoices learn-site omitted deductible_pct and took the 1.0
        NOT-NULL default; an explicit None would defeat that default and fail
        the flush, so the factory coalesces None -> 1.0."""
        rule = make_learned_vendor_rule(
            description="Anthropic, PBC",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )
        assert rule.deductible_pct == 1.0

    def test_explicit_deductible_pct_passes_through(self) -> None:
        """The transactions learn-sites passed tx.deductible_pct; a real value,
        including 0.0 (non-deductible), must survive — only None coalesces."""
        rule = make_learned_vendor_rule(
            description="Anthropic, PBC",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
            deductible_pct=0.5,
        )
        assert rule.deductible_pct == 0.5
        zero = make_learned_vendor_rule(
            description="Anthropic, PBC",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
            deductible_pct=0.0,
        )
        assert zero.deductible_pct == 0.0


class TestRepairWritesAuditTrail:
    """qreview P2-d1e: ``repair_literal_regex_rules`` flips ``is_regex`` on up
    to 34 rules — a field-level change to classification behavior. The system's
    critical rule is a full audit trail for every field-level change, so each
    flip must leave an entity-mode ``AuditEvent``. Without it, 34 rules change
    how money is categorized with no record of who, when, or what.
    """

    def _dead_regex_rule(self, session: Session, pattern: str) -> VendorRule:
        """A rule the ING-005 migration poisoned: a real regex stored literal."""
        return _rule(session, pattern, is_regex=False)

    def test_apply_writes_one_audit_row_per_repaired_rule(
        self, session: Session
    ) -> None:
        r1 = self._dead_regex_rule(session, CARDINAL_RULE)
        r2 = self._dead_regex_rule(session, r"\bshopify\b")
        self._rule_real_literal = _rule(session, "Anthropic Headquarters")

        result = repair_literal_regex_rules(
            session, dry_run=False, changed_by="cron:test_repair"
        )
        session.commit()

        audits = session.query(AuditEvent).all()
        assert result.repaired == 2
        assert len(audits) == 2
        by_entity = {a.entity_id: a for a in audits}
        assert set(by_entity) == {r1.id, r2.id}
        for a in audits:
            assert a.entity_type == ENTITY_TYPE_VENDOR_RULE
            assert a.field_changed == "is_regex"
            assert a.old_value == "False"
            assert a.new_value == "True"
            assert a.changed_by == "cron:test_repair"
            assert a.transaction_id is None  # entity mode, not transaction mode

    def test_dry_run_writes_no_audit_rows(self, session: Session) -> None:
        self._dead_regex_rule(session, CARDINAL_RULE)
        repair_literal_regex_rules(session, dry_run=True)
        assert session.query(AuditEvent).count() == 0

    def test_uncompilable_rule_is_not_audited(self, session: Session) -> None:
        """A skipped (non-compiling) rule is not mutated, so it earns no audit
        row — an audit trail records changes, not non-changes."""
        self._dead_regex_rule(session, r"cardinal.*health(")  # unbalanced paren
        result = repair_literal_regex_rules(session, dry_run=False)
        session.commit()
        assert result.repaired == 0
        assert result.skipped == 1
        assert session.query(AuditEvent).count() == 0


# ---------------------------------------------------------------------------
# accounting#82 (P2-c9d): force_literal escape hatch for a legitimate
# pipe/bracket literal, per REQ-VRESC-01..03.
# ---------------------------------------------------------------------------


class TestForceLiteralEscapeHatch:
    def test_force_literal_bypasses_looks_like_regex(self) -> None:
        """REQ-VRESC-01: force_literal=True skips looks_like_regex entirely
        for the is_regex=False path, so a real '|'-bearing descriptor can be
        stored as a literal without raising PatternFlagError."""
        validate_pattern_flag(
            "FOO|BAR LLC", is_regex=False, force_literal=True
        )  # must not raise

    def test_force_literal_matches_own_description(self, session: Session) -> None:
        """REQ-VRESC-02: a literal stored with force_literal=True matches its
        own description via re.escape, per the issue's acceptance line."""
        validate_pattern_flag(
            "FOO|BAR LLC", is_regex=False, force_literal=True
        )  # must not raise before the rule is even stored
        rule = _rule(session, "FOO|BAR LLC", is_regex=False)
        hit = lookup_vendor_rule("FOO|BAR LLC", session)
        assert hit is not None, "force_literal rule must match its own description"
        assert hit.id == rule.id

    def test_default_still_rejects_pipe_and_bracket_literal(self) -> None:
        """REQ-VRESC-03: the default (force_literal=False, today's call
        shape) still rejects '|'/'[...]' literals exactly as before."""
        with pytest.raises(PatternFlagError):
            validate_pattern_flag("FOO|BAR LLC", is_regex=False)
        with pytest.raises(PatternFlagError):
            validate_pattern_flag("ACME [WEST]", is_regex=False)
