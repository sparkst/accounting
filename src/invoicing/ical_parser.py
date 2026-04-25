"""iCal (.ics) parser for extracting billable calendar sessions.

REQ-INV-001: Parse iCal files, filter by customer patterns, handle recurring
events and timezone conversion, and return structured billable session data.

Usage::

    from src.invoicing.ical_parser import parse_ical

    with open("calendar.ics", "rb") as f:
        result = parse_ical(f.read(), customer, start_date, end_date)

    for session in result.matched_sessions:
        print(session["date"], session["duration_hours"], session["description"])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dateutil import rrule as dateutil_rrule
from icalendar import Calendar, Event, vDDDTypes

logger = logging.getLogger(__name__)

# Max file size: 10 MB
_MAX_FILE_SIZE = 10 * 1024 * 1024

# Max RRULE expansion to prevent runaway processing
_MAX_RRULE_EVENTS = 1000

# Pacific timezone (handles PST/PDT automatically via zoneinfo)
try:
    import zoneinfo

    _PACIFIC = zoneinfo.ZoneInfo("America/Los_Angeles")
except ImportError:
    # Python < 3.9 fallback: use dateutil's tzdata
    from dateutil.tz import gettz

    _PACIFIC = gettz("America/Los_Angeles")  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class BillableSession:
    """A single parsed calendar session that matched customer patterns."""

    date: str  # YYYY-MM-DD
    start_time: str  # HH:MM
    duration_hours: float
    description: str
    event_uid: str


@dataclass
class ParseResult:
    """Result returned by parse_ical()."""

    matched_sessions: list[BillableSession] = field(default_factory=list)
    unmatched_events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_pacific(dt: datetime) -> datetime:
    """Convert a timezone-aware or naive datetime to Pacific time.

    Naive datetimes are assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_PACIFIC)


def _is_all_day(dtstart: Any) -> bool:
    """Return True if the DTSTART component represents a DATE (not DATE-TIME)."""
    # icalendar represents all-day events as date objects, not datetime.
    val = dtstart.dt if isinstance(dtstart, vDDDTypes) else dtstart
    return isinstance(val, date) and not isinstance(val, datetime)


def _extract_dt(component_value: Any) -> datetime | None:
    """Extract a datetime from an icalendar component value.

    Returns None if the value is a DATE (all-day) rather than DATE-TIME.
    Naive datetimes are returned as-is (caller converts to Pacific).
    """
    val = component_value.dt if isinstance(component_value, vDDDTypes) else component_value
    if isinstance(val, datetime):
        return val
    # All-day (date only) — not a timed event
    return None


def _resolve_tz(tz_id: str | None, warnings: list[str]) -> Any:
    """Resolve a TZID string to a tzinfo object, falling back to UTC with a warning."""
    if not tz_id:
        return UTC
    try:
        import zoneinfo

        return zoneinfo.ZoneInfo(tz_id)
    except (ImportError, KeyError):
        pass
    try:
        from dateutil.tz import gettz

        result = gettz(tz_id)
        if result is not None:
            return result
    except Exception:
        pass
    warnings.append(f"Unrecognized timezone '{tz_id}' — falling back to UTC.")
    return UTC


def _matches_patterns(summary: str, patterns: list[str]) -> bool:
    """Return True if summary contains any of the patterns (case-insensitive)."""
    lower = summary.lower()
    return any(p.lower() in lower for p in patterns)


def _exdates_for_event(component: Event) -> set[date]:
    """Collect all EXDATE dates from a VEVENT component."""
    excluded: set[date] = set()
    exdate_prop = component.get("EXDATE")
    if exdate_prop is None:
        return excluded

    # EXDATE can be a single value or a list
    if not isinstance(exdate_prop, list):
        exdate_prop = [exdate_prop]

    for ex in exdate_prop:
        # ex.dts is a list of vDDDTypes
        dts = getattr(ex, "dts", [ex])
        for dt_val in dts:
            raw = getattr(dt_val, "dt", dt_val)
            if isinstance(raw, datetime):
                excluded.add(raw.date())
            elif isinstance(raw, date):
                excluded.add(raw)

    return excluded


def _duration_hours(dtstart: datetime, dtend: datetime | None) -> float:
    """Compute hours between start and end; default 1.0 if end is None."""
    if dtend is None:
        return 1.0
    delta = dtend - dtstart
    return round(delta.total_seconds() / 3600, 4)


# ---------------------------------------------------------------------------
# RRULE expansion
# ---------------------------------------------------------------------------


def _expand_rrule(
    component: Event,
    dtstart: datetime,
    start_date: date,
    end_date: date,
    warnings: list[str],
) -> list[datetime]:
    """Expand a RRULE into a list of datetimes within the date range.

    Caps expansion at _MAX_RRULE_EVENTS and adds a warning if truncated.
    """
    rrule_prop = component.get("RRULE")
    if rrule_prop is None:
        return []

    # Convert icalendar vRecur to rrulestr-compatible string
    rrule_str = rrule_prop.to_ical().decode()
    if not rrule_str.startswith("RRULE:"):
        rrule_str = f"RRULE:{rrule_str}"

    # We expand a window: start_date - 1 day to end_date + 1 day to catch boundary events
    window_start = datetime.combine(start_date - timedelta(days=1), time.min).replace(
        tzinfo=dtstart.tzinfo
    )
    window_end = datetime.combine(end_date + timedelta(days=1), time.max).replace(
        tzinfo=dtstart.tzinfo
    )

    try:
        rule = dateutil_rrule.rrulestr(
            rrule_str,
            dtstart=dtstart,
            ignoretz=False,
        )
    except Exception as exc:
        warnings.append(f"Could not parse RRULE '{rrule_str}': {exc}")
        return []

    results: list[datetime] = []
    for dt in rule:
        if dt > window_end:
            break
        if dt >= window_start:
            results.append(dt)
        if len(results) >= _MAX_RRULE_EVENTS:
            warnings.append(
                f"RRULE expansion exceeded {_MAX_RRULE_EVENTS} events — results truncated."
            )
            break

    return results


# ---------------------------------------------------------------------------
# Core deduplication key
# ---------------------------------------------------------------------------


def _dedup_key(session: BillableSession) -> tuple[str, str, str]:
    return (session.date, session.start_time, session.description)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_ical(
    ics_bytes: bytes,
    customer: Any,
    start_date: date,
    end_date: date,
) -> ParseResult:
    """Parse an iCal file and extract billable sessions for a customer.

    Args:
        ics_bytes:   Raw bytes of the .ics file.
        customer:    Customer ORM object with calendar_patterns and
                     calendar_exclusions attributes (both lists of strings).
        start_date:  First date of the billing period (inclusive).
        end_date:    Last date of the billing period (inclusive).

    Returns:
        ParseResult with matched_sessions, unmatched_events, and warnings.

    Raises:
        ValueError: If ics_bytes exceeds 10 MB.
    """
    if len(ics_bytes) > _MAX_FILE_SIZE:
        raise ValueError(
            f"iCal file is too large ({len(ics_bytes):,} bytes). Maximum is 10 MB."
        )

    result = ParseResult()
    patterns: list[str] = customer.calendar_patterns or []
    exclusions: list[str] = customer.calendar_exclusions or []

    try:
        cal = Calendar.from_ical(ics_bytes)
    except Exception as exc:
        result.warnings.append(f"Failed to parse iCal data: {exc}")
        return result

    # First pass: collect RECURRENCE-ID dates by UID.
    # Modified instances override RRULE-generated occurrences at those dates.
    recurrence_overrides: dict[str, set[date]] = {}
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        rid = component.get("RECURRENCE-ID")
        if rid is None:
            continue
        uid = str(component.get("UID", ""))
        rid_dt = _extract_dt(rid)
        if rid_dt is not None:
            if rid_dt.tzinfo is None:
                tz_id = getattr(rid, "params", {}).get("TZID")
                tz = _resolve_tz(tz_id, result.warnings)
                rid_dt = rid_dt.replace(tzinfo=tz)
            rid_date = _to_pacific(rid_dt).date()
            recurrence_overrides.setdefault(uid, set()).add(rid_date)

    seen_keys: set[tuple[str, str, str]] = set()

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # ── DTSTART ──────────────────────────────────────────────────────────
        dtstart_raw = component.get("DTSTART")
        if dtstart_raw is None:
            summary_hint = str(component.get("SUMMARY", "<no summary>"))
            result.warnings.append(
                f"Event '{summary_hint}' has no DTSTART — skipped."
            )
            continue

        # All-day events are not billable
        if _is_all_day(dtstart_raw):
            continue

        dtstart = _extract_dt(dtstart_raw)
        if dtstart is None:
            continue

        # Attach timezone if floating (naive)
        if dtstart.tzinfo is None:
            tz_id = getattr(dtstart_raw, "params", {}).get("TZID")
            tz = _resolve_tz(tz_id, result.warnings)
            dtstart = dtstart.replace(tzinfo=tz)

        # ── DTEND ────────────────────────────────────────────────────────────
        dtend_raw = component.get("DTEND")
        dtend: datetime | None = None
        if dtend_raw is not None:
            dtend = _extract_dt(dtend_raw)
            if dtend is not None and dtend.tzinfo is None:
                tz_id = getattr(dtend_raw, "params", {}).get("TZID")
                tz = _resolve_tz(tz_id, result.warnings)
                dtend = dtend.replace(tzinfo=tz)

        # ── SUMMARY & UID ─────────────────────────────────────────────────────
        summary = str(component.get("SUMMARY", "")).strip()
        uid = str(component.get("UID", ""))

        # ── RECURRENCE-ID handling ───────────────────────────────────────────
        has_recurrence_id = component.get("RECURRENCE-ID") is not None

        # ── EXDATE ────────────────────────────────────────────────────────────
        exdates = _exdates_for_event(component)

        # ── RRULE expansion vs. single occurrence ─────────────────────────────
        has_rrule = component.get("RRULE") is not None
        uid_overrides = recurrence_overrides.get(uid, set())

        if has_rrule:
            occurrences = _expand_rrule(
                component, dtstart, start_date, end_date, result.warnings
            )
        elif has_recurrence_id:
            occurrences = [dtstart]
        else:
            occurrences = [dtstart]

        for occ_start in occurrences:
            # Compute occurrence end by shifting dtend by the same offset
            if dtend is not None and has_rrule:
                offset = dtend - dtstart
                occ_end: datetime | None = occ_start + offset
            else:
                occ_end = dtend

            # Convert to Pacific time
            pac_start = _to_pacific(occ_start)
            occ_date = pac_start.date()

            # Skip if EXDATE
            if occ_date in exdates:
                continue

            # Skip RRULE occurrences overridden by a modified instance
            if has_rrule and occ_date in uid_overrides:
                continue

            # Filter by date range
            if not (start_date <= occ_date <= end_date):
                continue

            pac_end = _to_pacific(occ_end) if occ_end is not None else None
            hours = _duration_hours(pac_start, pac_end)
            start_time_str = pac_start.strftime("%H:%M")
            date_str = occ_date.strftime("%Y-%m-%d")

            # ── Pattern matching ──────────────────────────────────────────────
            matched = _matches_patterns(summary, patterns) if patterns else True
            excluded = _matches_patterns(summary, exclusions) if exclusions else False

            if matched and not excluded:
                session = BillableSession(
                    date=date_str,
                    start_time=start_time_str,
                    duration_hours=hours,
                    description=summary,
                    event_uid=uid,
                )
                key = _dedup_key(session)
                if key not in seen_keys:
                    seen_keys.add(key)
                    result.matched_sessions.append(session)
            else:
                # Unmatched (or excluded) — add to unmatched for user review
                result.unmatched_events.append(
                    {
                        "date": date_str,
                        "start_time": start_time_str,
                        "duration_hours": hours,
                        "description": summary,
                        "event_uid": uid,
                        "excluded": excluded,
                    }
                )

    # Sort matched sessions chronologically
    result.matched_sessions.sort(key=lambda s: (s.date, s.start_time))

    return result
