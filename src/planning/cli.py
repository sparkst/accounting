"""Planning CLI — `python -m src.planning <subcommand>`.

Subcommands:
    simulate     run the engine + persist (default) or --dry-run
    show-latest  pretty-print the most recent PlanningRun
    compare      diff survival across runs since a date
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import sys
from typing import Any

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.planning.engine import simulate_grid
from src.planning.inputs import load_live
from src.planning.merge import merge_live_into
from src.planning.models import PlanningRun
from src.planning.params import DEFAULTS, Params, ScenarioGrid

logger = logging.getLogger(__name__)

VALID_SOURCES = ("cli", "scheduled", "api")


def _open_session() -> Session:
    """Indirection so tests can monkeypatch DB access."""
    return SessionLocal()


def _parse_overrides(items: list[str]) -> dict[str, Any]:
    """Parse `--override key=value` pairs into a dict.

    Values are JSON-loaded so the user can pass numbers, true/false, or
    quoted strings. Unknown keys are validated downstream in merge_live_into.
    """
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--override expects KEY=VALUE; got {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v  # treat as string
    return out


def _serialize_params(p: Params) -> dict[str, Any]:
    return dataclasses.asdict(p)


def _serialize_live(live: object) -> dict[str, Any]:
    d: dict[str, Any] = dataclasses.asdict(live)  # type: ignore[call-overload]
    # date → ISO string for JSON
    if isinstance(d.get("latest_snapshot_date"), dt.date):
        d["latest_snapshot_date"] = d["latest_snapshot_date"].isoformat()
    return d


def _serialize_results(results: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON-serializable fields (paths ndarray) and convert tuples."""
    out: dict[str, Any] = {}
    for name, r in results.items():
        out[name] = {
            "survival": r.survival,
            "owed": r.owed,
            "ruined_early_count": r.ruined_early_count,
            "final_taxable_p50": r.final_taxable_p50,
            "final_retirement_p50": r.final_retirement_p50,
            "percentiles": {str(age): list(pcts) for age, pcts in r.percentiles.items()},
        }
    return out


def _cmd_simulate(args: argparse.Namespace) -> int:
    overrides = _parse_overrides(args.override)
    if args.n_sims:
        overrides["n_sims"] = args.n_sims
    source = args.source if args.source in VALID_SOURCES else "cli"

    sess = _open_session()
    try:
        as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
        try:
            live = load_live(sess, today=as_of)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        if live.staleness_warning:
            print(f"WARNING: {live.staleness_warning}", file=sys.stderr)

        try:
            params = merge_live_into(DEFAULTS, live, overrides)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

        grid = ScenarioGrid.default()
        if args.scenarios:
            wanted = set(args.scenarios.split(","))
            grid = ScenarioGrid(
                scenarios=tuple(s for s in grid.scenarios if s.name in wanted)
            )
            if not grid.scenarios:
                print(
                    f"ERROR: no scenarios matched {args.scenarios!r}",
                    file=sys.stderr,
                )
                return 2

        results = simulate_grid(params, grid, seed=args.seed)

        # Pretty summary to stdout
        run_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        print(f"Planning run @ {run_at.isoformat()}Z (source={source})")
        print(f"  pool_taxable={params.pool_taxable:,.0f}  pool_retirement={params.pool_retirement:,.0f}")
        if live.staleness_warning:
            print(f"  WARNING: {live.staleness_warning}")
        print()
        print(f"  {'scenario':<45} survival")
        for name in sorted(results):
            r = results[name]
            marker = " !" if r.ruined_early_count > 0 else ""
            print(f"  {name:<45} {r.survival:>6.1%}{marker}")
        print()
        print("  live drift (informational):")
        print(f"    ttm_spend         = {live.ttm_spend:,.0f}   (planning spend_start={params.spend_start:,.0f})")
        print(f"    ttm_biz_income    = {live.ttm_biz_income:,.0f}   (planning biz_income={params.biz_income:,.0f})")
        print(f"    ttm_personal_income = {live.ttm_personal_income:,.0f}   (planning amy_wage_income={params.amy_wage_income:,.0f})")

        if args.dry_run:
            print("\n[dry-run -- not persisting]")
            return 0

        row = PlanningRun(
            run_at=run_at,
            source=source,
            params_json=_serialize_params(params),
            live_inputs_json=_serialize_live(live),
            scenarios_json=_serialize_results(results),
            notes=args.note,
        )
        sess.add(row)
        sess.commit()
        print(f"\n[persisted as PlanningRun id={row.id}]")
        return 0
    finally:
        sess.close()


def _cmd_show_latest(args: argparse.Namespace) -> int:
    sess = _open_session()
    try:
        row = (
            sess.query(PlanningRun)
            .order_by(PlanningRun.run_at.desc())
            .first()
        )
        if row is None:
            print("no planning runs yet -- try `simulate` first")
            return 1
        print(f"Run id={row.id}  run_at={row.run_at.isoformat()}Z  source={row.source}")
        if row.notes:
            print(f"Notes: {row.notes}")
        print()
        print(f"  {'scenario':<45} survival")
        for name in sorted(row.scenarios_json):
            r = row.scenarios_json[name]
            print(f"  {name:<45} {r['survival']:>6.1%}")
        return 0
    finally:
        sess.close()


def _cmd_compare(args: argparse.Namespace) -> int:
    since = dt.date.fromisoformat(args.since)
    sess = _open_session()
    try:
        rows = (
            sess.query(PlanningRun)
            .filter(PlanningRun.run_at >= dt.datetime.combine(since, dt.time.min))
            .order_by(PlanningRun.run_at.asc())
            .all()
        )
        if len(rows) < 2:
            print(f"need >=2 runs since {args.since} to compare; found {len(rows)}")
            return 1

        first = rows[0]
        last = rows[-1]
        print(f"Comparing  {first.run_at.date()}  ->  {last.run_at.date()}")
        print()
        print(f"  {'scenario':<45} {'first':>8} {'last':>8} {'delta':>8}")
        for name in sorted(last.scenarios_json):
            if name not in first.scenarios_json:
                continue
            sv_first = first.scenarios_json[name]["survival"]
            sv_last = last.scenarios_json[name]["survival"]
            delta = sv_last - sv_first
            arrow = "^" if delta > 0 else "v" if delta < 0 else " "
            print(f"  {name:<45} {sv_first:>7.1%} {sv_last:>7.1%} {delta:>+7.1%} {arrow}")
        return 0
    finally:
        sess.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.planning")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="run the engine and persist")
    sim.add_argument("--dry-run", action="store_true", help="do not persist")
    sim.add_argument(
        "--override", action="append", default=[],
        help="KEY=VALUE override (repeatable); JSON-parsed",
    )
    sim.add_argument(
        "--scenarios", default="",
        help="comma-separated subset of scenario names",
    )
    sim.add_argument("--note", default=None, help="tag the persisted run")
    sim.add_argument("--source", default="cli", choices=list(VALID_SOURCES))
    sim.add_argument("--n-sims", type=int, default=None, help="override n_sims")
    sim.add_argument(
        "--as-of", default=None,
        help="ISO date anchoring the live-input TTM windows (default: today)",
    )
    sim.add_argument("--seed", type=int, default=42)
    sim.set_defaults(func=_cmd_simulate)

    show = sub.add_parser("show-latest", help="pretty-print most recent run")
    show.set_defaults(func=_cmd_show_latest)

    cmp = sub.add_parser("compare", help="diff survival across runs since a date")
    cmp.add_argument("--since", required=True, help="ISO date, e.g. 2026-01-01")
    cmp.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
