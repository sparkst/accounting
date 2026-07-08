"""Shadow-run harness (REQ-VIS-002 / REQ-VIS-004).

Runs the legacy extractor and the vision provider over the SAME statement,
diffs the two field sets, writes a full diff-report JSON (including the raw
provider response for ``raw_data``-style provenance) plus one ``IngestionLog``
row per file, and enforces a hard per-run cost ceiling.

**Never writes register, brokerage, or history tables** — by construction (this
module opens no write path to them; it only writes ``ingestion_log`` +
``llm_usage_log`` rows and diff-report files) and by test (row-count invariant).

Cost ceiling (REQ-VIS-004):

* ``--max-files`` (default from ``config/vision.yaml``) bounds the batch.
* A pre-run projection (``files × est_tokens/file × provider rate``) ABORTS the
  whole run if it exceeds the cap — no provider call is made.
* During the run the cumulative cost from provider usage metadata is tracked and
  the batch STOPS the moment the next file would cross the cap; remaining files
  are marked ``skipped_cost_cap`` (per-file isolation preserved).

The error-tripped circuit breaker does not bound spend — this cap does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog
from src.vision import extract as extract_mod
from src.vision import schemas
from src.vision.client import VisionError, VisionProvider, select_provider
from src.vision.diff import DiffReport, diff_fields

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SHADOW_SOURCE = "vision_shadow"
_DEFAULT_REPORT_DIR = Path("data/vision-shadow")
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "vision.yaml"


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VisionConfig:
    """Cost-model + guardrail settings loaded from ``config/vision.yaml``."""

    run_cost_cap_usd: float
    max_files: int
    est_input_tokens: int
    est_output_tokens: int
    provider_rates: dict[str, tuple[float, float]]  # name -> (in_per_m, out_per_m)

    def per_file_projection(self, provider_name: str) -> float:
        """Projected USD cost for one file under *provider_name*'s config rates."""
        in_rate, out_rate = self.provider_rates.get(provider_name, (0.30, 2.50))
        return (
            self.est_input_tokens * in_rate + self.est_output_tokens * out_rate
        ) / 1_000_000


def load_config(path: Path | None = None) -> VisionConfig:
    """Load and validate ``config/vision.yaml`` into a :class:`VisionConfig`."""
    cfg_path = path or _CONFIG_PATH
    with cfg_path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    est = raw.get("est_tokens_per_file", {}) or {}
    providers = raw.get("providers", {}) or {}
    rates: dict[str, tuple[float, float]] = {}
    for name, pcfg in providers.items():
        rates[name] = (
            float(pcfg.get("input_per_m", 0.0)),
            float(pcfg.get("output_per_m", 0.0)),
        )
    return VisionConfig(
        run_cost_cap_usd=float(raw.get("run_cost_cap_usd", 2.00)),
        max_files=int(raw.get("max_files", 10)),
        est_input_tokens=int(est.get("input", 4000)),
        est_output_tokens=int(est.get("output", 1500)),
        provider_rates=rates,
    )


def resolve_cost_cap(config: VisionConfig, override: float | None = None) -> float:
    """Effective per-run cap: explicit override → env → config default."""
    if override is not None:
        return override
    env_val = os.environ.get("VISION_RUN_COST_CAP_USD")
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            logger.warning("bad VISION_RUN_COST_CAP_USD=%r; using config", env_val)
    return config.run_cost_cap_usd


# ── Inputs / results ─────────────────────────────────────────────────────────


@dataclass
class ShadowFile:
    """One statement to shadow-run: raw bytes + the pre-computed legacy fields."""

    name: str
    file_bytes: bytes
    mime: str
    legacy_fields: dict[str, Any]


# Per-file statuses.
OK = "ok"
ERROR = "error"
SKIPPED_COST_CAP = "skipped_cost_cap"


@dataclass
class ShadowFileResult:
    name: str
    status: str
    diff: DiffReport | None = None
    cost: float = 0.0
    error: str | None = None
    report_path: str | None = None


@dataclass
class ShadowBatchResult:
    institution: str
    provider: str
    files: list[ShadowFileResult] = field(default_factory=list)
    total_cost: float = 0.0
    aborted_projection: bool = False

    @property
    def n_processed(self) -> int:
        return sum(1 for f in self.files if f.status in (OK, ERROR))

    @property
    def n_skipped(self) -> int:
        return sum(1 for f in self.files if f.status == SKIPPED_COST_CAP)


# ── Report writing ───────────────────────────────────────────────────────────


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _write_report(
    report_dir: Path,
    *,
    institution: str,
    provider_name: str,
    statement_type: str,
    legacy_fields: dict[str, Any],
    vision_fields: dict[str, Any],
    raw_response: Any,
    diff: DiffReport,
    cost: float,
    file_bytes: bytes,
) -> Path:
    """Write the full diff-report JSON (incl. raw provider response) and return its path."""
    out_dir = report_dir / institution
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    path = out_dir / f"{ts}-{_file_hash(file_bytes)}.json"
    payload = {
        "institution": institution,
        "provider": provider_name,
        "statement_type": statement_type,
        "generated_at": datetime.now(UTC).isoformat(),
        "cost_estimate": cost,
        "legacy_fields": _jsonable(legacy_fields),
        "vision_fields": _jsonable(vision_fields),
        "raw_response": raw_response,  # REQ-VIS-004 provenance
        "diff": diff.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(fields, default=str))
    return out


def _summary_line(diff: DiffReport, provider_name: str, cost: float) -> str:
    return (
        f"{diff.n_match} match / {diff.n_mismatch} mismatch / "
        f"clean={diff.clean} / provider={provider_name} / cost=${cost:.6f}"
    )


# ── Core harness ─────────────────────────────────────────────────────────────


def run_shadow_batch(
    institution: str,
    files: list[ShadowFile],
    *,
    provider: VisionProvider,
    session: Session | None = None,
    config: VisionConfig | None = None,
    cost_cap: float | None = None,
    max_files: int | None = None,
    report_dir: Path | None = None,
) -> ShadowBatchResult:
    """Shadow-run a batch of statements for *institution*.

    Writes one diff-report JSON per successfully-extracted file plus one
    ``IngestionLog`` row per processed file. Enforces ``max_files`` truncation,
    the pre-run projection abort, and the mid-run cumulative cost stop.
    """
    cfg = config or load_config()
    spec = extract_mod.spec_for(institution)
    cap = resolve_cost_cap(cfg, cost_cap)
    limit = max_files if max_files is not None else cfg.max_files
    out_dir = report_dir or _DEFAULT_REPORT_DIR
    provider_name = getattr(provider, "name", "gemini")

    batch = ShadowBatchResult(institution=institution, provider=provider_name)

    # ── max-files truncation ────────────────────────────────────────────────
    selected = files[:limit]

    # ── pre-run projection abort ────────────────────────────────────────────
    per_file = cfg.per_file_projection(provider_name)
    projected = per_file * len(selected)
    if projected > cap:
        batch.aborted_projection = True
        logger.warning(
            "vision shadow ABORT: projection $%.4f (%d files) exceeds cap $%.4f",
            projected,
            len(selected),
            cap,
        )
        for f in selected:
            batch.files.append(ShadowFileResult(name=f.name, status=SKIPPED_COST_CAP))
        return batch

    running = 0.0
    stopped = False
    for f in selected:
        # Mid-run stop: if the next file's projection would cross the cap, skip
        # it and every remaining file (per-file isolation preserved).
        if stopped or running + per_file > cap:
            stopped = True
            batch.files.append(ShadowFileResult(name=f.name, status=SKIPPED_COST_CAP))
            continue

        result = _run_one(
            institution,
            spec,
            f,
            provider=provider,
            session=session,
            out_dir=out_dir,
        )
        batch.files.append(result)
        running += result.cost
        batch.total_cost += result.cost

    return batch


def _run_one(
    institution: str,
    spec: extract_mod.InstitutionSpec,
    f: ShadowFile,
    *,
    provider: VisionProvider,
    session: Session | None,
    out_dir: Path,
) -> ShadowFileResult:
    """Extract + diff one file. Per-file isolation: any failure → status=error."""
    provider_name = getattr(provider, "name", "gemini")
    legacy_fields = schemas.normalize_fields(spec.statement_type, f.legacy_fields)
    try:
        extraction = provider.extract(
            f.file_bytes, f.mime, spec.schema, spec.prompt, session=session
        )
        vision_fields = schemas.normalize_fields(spec.statement_type, extraction.fields)
        diff = diff_fields(legacy_fields, vision_fields)
        report_path = _write_report(
            out_dir,
            institution=institution,
            provider_name=provider_name,
            statement_type=spec.statement_type,
            legacy_fields=legacy_fields,
            vision_fields=vision_fields,
            raw_response=extraction.raw_response,
            diff=diff,
            cost=extraction.cost_estimate,
            file_bytes=f.file_bytes,
        )
        _log_ingestion(
            session,
            records_processed=1,
            records_failed=0,
            status=IngestionStatus.SUCCESS,
            detail=_summary_line(diff, provider_name, extraction.cost_estimate),
        )
        return ShadowFileResult(
            name=f.name,
            status=OK,
            diff=diff,
            cost=extraction.cost_estimate,
            report_path=str(report_path),
        )
    except (VisionError, ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("vision shadow: file %s failed: %s", f.name, exc)
        _log_ingestion(
            session,
            records_processed=1,
            records_failed=1,
            status=IngestionStatus.FAILURE,
            detail=f"{f.name}: {exc} / provider={provider_name}",
        )
        return ShadowFileResult(name=f.name, status=ERROR, error=str(exc))


def _log_ingestion(
    session: Session | None,
    *,
    records_processed: int,
    records_failed: int,
    status: IngestionStatus,
    detail: str,
) -> None:
    """Write one ``vision_shadow`` IngestionLog row (no-op without a session)."""
    if session is None:
        return
    try:
        session.add(
            IngestionLog(
                source=SHADOW_SOURCE,
                status=status.value,
                records_processed=records_processed,
                records_failed=records_failed,
                error_detail=detail,
                retryable=False,
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 — logging must not break the run
        logger.error("Failed to write vision_shadow IngestionLog: %s", exc)


# ── Legacy-side loading for the CLI (real files) ─────────────────────────────


def _load_legacy_fields(institution: str, path: Path) -> dict[str, Any]:
    """Build the legacy-side fields from a real file for the CLI path.

    PDF/text institutions are read via ``pdftotext_layout``; the value-fed
    carriers (nw_mutual, na_iul) are not supported from a bare file here and
    raise a clear error directing the operator to the values-driven API.
    """
    from src.adapters._shared.pdf import pdftotext_layout

    if institution in (extract_mod.FG, extract_mod.GSK):
        text = pdftotext_layout(path)
        return extract_mod.legacy_extract(institution, text=text)
    if institution == extract_mod.FT:
        text = pdftotext_layout(path)
        return extract_mod.legacy_extract(institution, text=text, filename=path.name)
    raise ValueError(
        f"CLI file mode not supported for {institution!r}; drive nw_mutual/na_iul "
        "via the values-based API (src.vision.shadow.run_shadow_batch)."
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.vision.shadow",
        description="Run vision extraction in SHADOW MODE against the legacy parser.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="Shadow-run one statement file.")
    s.add_argument(
        "--institution",
        required=True,
        choices=list(extract_mod.INSTITUTIONS),
    )
    s.add_argument("--file", required=True, help="Path to the statement file.")
    s.add_argument("--provider", default=None, help="gemini | openai (default env).")
    s.add_argument("--max-files", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — CLI wiring
    args = _build_arg_parser().parse_args(argv)
    if args.cmd != "run":
        return 2

    path = Path(args.file)
    try:
        legacy_fields = _load_legacy_fields(args.institution, path)
        file_bytes = path.read_bytes()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"legacy extraction failed: {exc}", file=sys.stderr)
        return 1

    provider = select_provider(args.provider)
    mime = "application/pdf"
    shadow_file = ShadowFile(
        name=path.name, file_bytes=file_bytes, mime=mime, legacy_fields=legacy_fields
    )

    try:
        from src.db.connection import get_session
    except ImportError:
        session_cm = None
    else:
        session_cm = get_session()

    if session_cm is None:
        batch = run_shadow_batch(
            args.institution, [shadow_file], provider=provider, max_files=args.max_files
        )
    else:
        with session_cm as session:
            batch = run_shadow_batch(
                args.institution,
                [shadow_file],
                provider=provider,
                session=session,
                max_files=args.max_files,
            )
            session.commit()

    for fr in batch.files:
        if fr.status == OK and fr.diff is not None:
            print(f"{fr.name}: {_summary_line(fr.diff, batch.provider, fr.cost)}")
            print(f"  report: {fr.report_path}")
        elif fr.status == SKIPPED_COST_CAP:
            print(f"{fr.name}: skipped (cost cap)")
        else:
            print(f"{fr.name}: ERROR — {fr.error}")
    if batch.aborted_projection:
        print("run aborted: pre-run projection exceeds cost cap")
    return 0


# Expose date for callers building value-fed legacy fields.
__all__ = [
    "ERROR",
    "OK",
    "SHADOW_SOURCE",
    "SKIPPED_COST_CAP",
    "ShadowBatchResult",
    "ShadowFile",
    "ShadowFileResult",
    "VisionConfig",
    "date",
    "load_config",
    "resolve_cost_cap",
    "run_shadow_batch",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
