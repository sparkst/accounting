"""Tests for src/invoicing/ical_parser.py.

REQ-INV-001: Parse iCal, filter by customer patterns, handle recurring events,
timezone conversion, EXDATE, deduplication, and edge cases.

Tests use synthetic .ics content as fixtures — no external files required.
"""

from __future__ import annotations

import textwrap
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from src.invoicing.ical_parser import BillableSession, ParseResult, parse_ical

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_customer(
    patterns: list[str] | None = None,
    exclusions: list[str] | None = None,
) -> Any:
    """Return a minimal customer-like object for parse_ical()."""
    return SimpleNamespace(
        calendar_patterns=patterns or [],
        calendar_exclusions=exclusions or [],
    )


def fascinate_customer() -> Any:
    return make_customer(
        patterns=["Ben / Travis", "Fascinate OS", "Fascinate"],
        exclusions=["Book with Ben"],
    )


def wrap_ics(vevent_body: str) -> bytes:
    """Wrap VEVENT content in a valid iCal calendar envelope.

    Produces strict RFC 5545 line endings (CRLF) and no leading whitespace.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Test//Test//EN",
    ]
    for line in vevent_body.strip().splitlines():
        lines.append(line.strip())
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode()


def single_event(
    uid: str = "test-uid-1@test",
    summary: str = "Fascinate OS sync",
    dtstart: str = "20260305T180000Z",
    dtend: str = "20260305T190000Z",
) -> str:
    return "\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{summary}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            "END:VEVENT",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Basic parsing — matching
# ---------------------------------------------------------------------------


class TestBasicParsing:
    def test_single_matching_event_returned(self) -> None:
        """An event whose summary matches a pattern is included in matched_sessions."""
        ics = wrap_ics(
            single_event(summary="Fascinate OS sync", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1
        session = result.matched_sessions[0]
        assert session.description == "Fascinate OS sync"
        assert session.duration_hours == 1.0

    def test_event_outside_date_range_excluded(self) -> None:
        """Events before start_date or after end_date are not included."""
        ics = wrap_ics(
            single_event(dtstart="20260201T180000Z", dtend="20260201T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 0

    def test_non_matching_summary_goes_to_unmatched(self) -> None:
        """Events that don't match customer patterns appear in unmatched_events."""
        ics = wrap_ics(
            single_event(summary="Team standup", dtstart="20260305T150000Z", dtend="20260305T153000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 0
        assert len(result.unmatched_events) == 1
        assert result.unmatched_events[0]["description"] == "Team standup"

    def test_case_insensitive_pattern_match(self) -> None:
        """Pattern matching is case-insensitive."""
        ics = wrap_ics(
            single_event(summary="fascinate os planning", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1

    def test_exclusion_pattern_removes_match(self) -> None:
        """Events matching an exclusion pattern are removed even if they match a pattern."""
        ics = wrap_ics(
            single_event(summary="Book with Ben", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 0

    def test_exclusion_lands_in_unmatched_with_excluded_flag(self) -> None:
        """Excluded events appear in unmatched_events with excluded=True."""
        ics = wrap_ics(
            single_event(summary="Book with Ben", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert any(e["excluded"] for e in result.unmatched_events)

    def test_partial_substring_match(self) -> None:
        """Pattern 'Fascinate' matches 'Fascinate OS weekly sync'."""
        ics = wrap_ics(
            single_event(summary="Fascinate OS weekly sync", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1


# ---------------------------------------------------------------------------
# Timezone conversion
# ---------------------------------------------------------------------------


class TestTimezoneConversion:
    def test_utc_converted_to_pacific(self) -> None:
        """UTC 01:00 on March 6 converts to March 5 17:00 Pacific (PST, UTC-8)."""
        # 2026-03-06T01:00:00Z → 2026-03-05T17:00:00 PST
        ics = wrap_ics(
            single_event(
                summary="Fascinate OS sync",
                dtstart="20260306T010000Z",
                dtend="20260306T020000Z",
            )
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1
        session = result.matched_sessions[0]
        # After conversion to Pacific: date is March 5, time is 17:00
        assert session.date == "2026-03-05"
        assert session.start_time == "17:00"

    def test_pdt_dst_aware_conversion(self) -> None:
        """UTC to Pacific during DST (PDT, UTC-7): 20:00 UTC → 13:00 PDT."""
        # 2026-07-15T20:00:00Z → 2026-07-15T13:00:00 PDT (UTC-7)
        ics = wrap_ics(
            single_event(
                summary="Ben / Travis weekly",
                dtstart="20260715T200000Z",
                dtend="20260715T210000Z",
            )
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 7, 1), date(2026, 7, 31))
        assert len(result.matched_sessions) == 1
        session = result.matched_sessions[0]
        assert session.date == "2026-07-15"
        assert session.start_time == "13:00"

    def test_named_timezone_in_dtstart(self) -> None:
        """Events with TZID parameter are converted correctly."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:tz-test@test
            SUMMARY:Fascinate OS sync
            DTSTART;TZID=America/New_York:20260305T110000
            DTEND;TZID=America/New_York:20260305T120000
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1
        session = result.matched_sessions[0]
        # 11:00 ET = 08:00 PT (PST, UTC-8)
        assert session.start_time == "08:00"


# ---------------------------------------------------------------------------
# All-day events
# ---------------------------------------------------------------------------


class TestAllDayEvents:
    def test_all_day_event_excluded(self) -> None:
        """All-day events (DATE, not DATE-TIME) are not included in results."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:allday@test
            SUMMARY:Fascinate OS offsite
            DTSTART;VALUE=DATE:20260305
            DTEND;VALUE=DATE:20260306
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 0
        assert len(result.unmatched_events) == 0

    def test_all_day_event_not_in_unmatched(self) -> None:
        """All-day events do not appear in unmatched_events either."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:allday2@test
            SUMMARY:Random holiday
            DTSTART;VALUE=DATE:20260310
            DTEND;VALUE=DATE:20260311
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.unmatched_events) == 0


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


class TestDuration:
    def test_1_hour_meeting_duration(self) -> None:
        ics = wrap_ics(
            single_event(dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert result.matched_sessions[0].duration_hours == 1.0

    def test_90_minute_meeting_duration(self) -> None:
        ics = wrap_ics(
            single_event(dtstart="20260305T180000Z", dtend="20260305T193000Z")
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert result.matched_sessions[0].duration_hours == 1.5

    def test_missing_dtend_defaults_to_1_hour(self) -> None:
        """Events without DTEND default to 1-hour duration."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:no-end@test
            SUMMARY:Fascinate OS sync
            DTSTART:20260305T180000Z
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1
        assert result.matched_sessions[0].duration_hours == 1.0


# ---------------------------------------------------------------------------
# Missing DTSTART warning
# ---------------------------------------------------------------------------


class TestMissingDtstart:
    def test_event_without_dtstart_gets_warning(self) -> None:
        """Events missing DTSTART are skipped and produce a warning."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:no-start@test
            SUMMARY:Fascinate OS sync
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 0
        assert any("no DTSTART" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# RRULE (recurring events)
# ---------------------------------------------------------------------------


class TestRrule:
    def test_weekly_recurring_events_expanded(self) -> None:
        """Weekly RRULE events within the date range are all returned."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:weekly@test
            SUMMARY:Ben / Travis weekly
            DTSTART:20260302T180000Z
            DTEND:20260302T190000Z
            RRULE:FREQ=WEEKLY;COUNT=5
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        # March 2, 9, 16, 23, 30 (all in March in UTC — converted to Pacific = March 2-30)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 5

    def test_recurring_event_outside_range_excluded(self) -> None:
        """RRULE occurrences outside the date range are not included."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:weekly-limited@test
            SUMMARY:Fascinate OS sync
            DTSTART:20260302T180000Z
            DTEND:20260302T190000Z
            RRULE:FREQ=WEEKLY;COUNT=10
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        # Filter to only March
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        for session in result.matched_sessions:
            assert session.date.startswith("2026-03")

    def test_rrule_respects_duration_per_occurrence(self) -> None:
        """Duration is consistent across all RRULE occurrences."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:weekly-dur@test
            SUMMARY:Ben / Travis weekly
            DTSTART:20260302T180000Z
            DTEND:20260302T200000Z
            RRULE:FREQ=WEEKLY;COUNT=3
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert all(s.duration_hours == 2.0 for s in result.matched_sessions)


# ---------------------------------------------------------------------------
# EXDATE (cancelled occurrences)
# ---------------------------------------------------------------------------


class TestExdate:
    def test_exdate_occurrence_skipped(self) -> None:
        """EXDATE cancellations remove that occurrence from results."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:weekly-exdate@test
            SUMMARY:Ben / Travis weekly
            DTSTART:20260302T180000Z
            DTEND:20260302T190000Z
            RRULE:FREQ=WEEKLY;COUNT=4
            EXDATE:20260309T180000Z
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        # 4 occurrences, 1 cancelled → expect 3
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        dates = [s.date for s in result.matched_sessions]
        assert "2026-03-09" not in dates
        assert len(result.matched_sessions) == 3


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_events_deduplicated(self) -> None:
        """Two events with identical date+time+description are merged into one."""
        event_block = single_event(
            uid="dup-1@test",
            summary="Fascinate OS sync",
            dtstart="20260305T180000Z",
            dtend="20260305T190000Z",
        ) + single_event(
            uid="dup-2@test",
            summary="Fascinate OS sync",
            dtstart="20260305T180000Z",
            dtend="20260305T190000Z",
        )
        ics = wrap_ics(event_block)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1

    def test_different_times_not_deduplicated(self) -> None:
        """Events at different times on the same day are NOT deduplicated."""
        event_block = single_event(
            uid="diff-1@test",
            summary="Fascinate OS sync",
            dtstart="20260305T160000Z",
            dtend="20260305T170000Z",
        ) + single_event(
            uid="diff-2@test",
            summary="Fascinate OS sync",
            dtstart="20260305T180000Z",
            dtend="20260305T190000Z",
        )
        ics = wrap_ics(event_block)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 2


# ---------------------------------------------------------------------------
# File size validation
# ---------------------------------------------------------------------------


class TestFileSizeValidation:
    def test_oversized_file_raises_value_error(self) -> None:
        """Files over 10 MB raise ValueError before any parsing."""
        big_bytes = b"X" * (10 * 1024 * 1024 + 1)
        with pytest.raises(ValueError, match="too large"):
            parse_ical(big_bytes, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))

    def test_exactly_10mb_allowed(self) -> None:
        """Files exactly at the limit do not raise ValueError (though they may fail to parse)."""
        exactly_10mb = b"X" * (10 * 1024 * 1024)
        # Will not raise ValueError — may raise parse warning
        try:
            parse_ical(exactly_10mb, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        except ValueError as exc:
            # Should not be a size error
            assert "too large" not in str(exc)


# ---------------------------------------------------------------------------
# Session fields
# ---------------------------------------------------------------------------


class TestSessionFields:
    def test_session_has_required_fields(self) -> None:
        """Each BillableSession has date, start_time, duration_hours, description, event_uid."""
        ics = wrap_ics(
            single_event(
                uid="fields-test@test",
                summary="Ben / Travis planning",
                dtstart="20260305T180000Z",
                dtend="20260305T190000Z",
            )
        )
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1
        s = result.matched_sessions[0]
        assert isinstance(s, BillableSession)
        assert s.date == "2026-03-05"
        assert s.start_time == "10:00"  # 18:00 UTC = 10:00 PST
        assert s.duration_hours == 1.0
        assert s.description == "Ben / Travis planning"
        assert s.event_uid == "fields-test@test"

    def test_date_format_is_iso(self) -> None:
        """date field uses YYYY-MM-DD format."""
        ics = wrap_ics(single_event(dtstart="20260310T180000Z", dtend="20260310T190000Z"))
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert result.matched_sessions[0].date == "2026-03-10"

    def test_start_time_format_is_hhmm(self) -> None:
        """start_time field uses HH:MM format."""
        # March 10 2026 is after DST starts (Mar 8) so Pacific is PDT (UTC-7)
        # 21:00 UTC = 14:00 PDT
        ics = wrap_ics(single_event(dtstart="20260310T210000Z", dtend="20260310T220000Z"))
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert result.matched_sessions[0].start_time == "14:00"


# ---------------------------------------------------------------------------
# Chronological ordering
# ---------------------------------------------------------------------------


class TestSorting:
    def test_sessions_sorted_chronologically(self) -> None:
        """matched_sessions are returned in ascending date+time order."""
        event_block = (
            single_event(uid="late@test", summary="Fascinate OS sync", dtstart="20260320T180000Z", dtend="20260320T190000Z")
            + single_event(uid="early@test", summary="Ben / Travis", dtstart="20260305T180000Z", dtend="20260305T190000Z")
            + single_event(uid="mid@test", summary="Fascinate planning", dtstart="20260312T180000Z", dtend="20260312T190000Z")
        )
        ics = wrap_ics(event_block)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        dates = [s.date for s in result.matched_sessions]
        assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# Multiple patterns
# ---------------------------------------------------------------------------


class TestMultiplePatterns:
    def test_any_pattern_triggers_match(self) -> None:
        """Events matching any of the customer patterns are all included."""
        event_block = (
            single_event(uid="p1@test", summary="Ben / Travis sync", dtstart="20260305T180000Z", dtend="20260305T190000Z")
            + single_event(uid="p2@test", summary="Fascinate OS planning", dtstart="20260310T180000Z", dtend="20260310T190000Z")
            + single_event(uid="p3@test", summary="Fascinate roadmap review", dtstart="20260315T180000Z", dtend="20260315T190000Z")
        )
        ics = wrap_ics(event_block)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 3


# ---------------------------------------------------------------------------
# Empty / malformed input
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_calendar_returns_empty_result(self) -> None:
        """A calendar with no VEVENTs returns empty results."""
        ics = textwrap.dedent(
            """\
            BEGIN:VCALENDAR
            VERSION:2.0
            PRODID:-//Test//Test//EN
            END:VCALENDAR
            """
        ).encode()
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert result.matched_sessions == []
        assert result.unmatched_events == []
        assert result.warnings == []

    def test_malformed_ical_returns_warning(self) -> None:
        """Invalid iCal data returns a warning instead of raising."""
        result = parse_ical(b"NOT VALID ICAL DATA", fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert any("parse" in w.lower() or "ical" in w.lower() for w in result.warnings)

    def test_no_patterns_matches_everything(self) -> None:
        """When customer has no patterns, all events are treated as matched."""
        customer = make_customer(patterns=[], exclusions=[])
        ics = wrap_ics(
            single_event(summary="Any meeting", dtstart="20260305T180000Z", dtend="20260305T190000Z")
        )
        result = parse_ical(ics, customer, date(2026, 3, 1), date(2026, 3, 31))
        assert len(result.matched_sessions) == 1

    def test_result_is_parse_result_instance(self) -> None:
        """parse_ical always returns a ParseResult."""
        ics = wrap_ics(single_event())
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert isinstance(result, ParseResult)

    def test_unrecognized_timezone_warning(self) -> None:
        """Events with unrecognized TZID produce a warning and fall back to UTC."""
        vevent = textwrap.dedent(
            """\
            BEGIN:VEVENT
            UID:badtz@test
            SUMMARY:Fascinate OS sync
            DTSTART;TZID=Bogus/Nowhere:20260305T180000
            DTEND;TZID=Bogus/Nowhere:20260305T190000
            END:VEVENT
            """
        )
        ics = wrap_ics(vevent)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        assert any("Unrecognized timezone" in w for w in result.warnings)
        # Event still parsed (fell back to UTC)
        assert len(result.matched_sessions) == 1

    def test_boundary_dates_inclusive(self) -> None:
        """Events exactly on start_date and end_date are included."""
        event_block = (
            single_event(uid="start@test", summary="Fascinate OS sync", dtstart="20260301T180000Z", dtend="20260301T190000Z")
            + single_event(uid="end@test", summary="Fascinate OS sync", dtstart="20260331T180000Z", dtend="20260331T190000Z")
        )
        ics = wrap_ics(event_block)
        result = parse_ical(ics, fascinate_customer(), date(2026, 3, 1), date(2026, 3, 31))
        dates = {s.date for s in result.matched_sessions}
        assert "2026-03-01" in dates
        assert "2026-03-31" in dates
