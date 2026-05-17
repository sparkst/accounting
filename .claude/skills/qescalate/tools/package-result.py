#!/usr/bin/env python3
"""
Package the final qescalate result — either a converged decision or an escalation
to Travis. Aggregates proposal + validator + round1 + round2 + convergence into
a single structured output the calling agent reads.

Usage:
    python3 tools/package-result.py \
        --proposal /tmp/qescalate-proposal.json \
        --validator /tmp/qescalate-validation.json \
        --council /tmp/qescalate-council.json \
        --convergence /tmp/qescalate-convergence.json \
        --round1-dir /tmp \
        --round2-dir /tmp \
        --out /tmp/qescalate-result.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_json(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def load_round(dir_path: Path, n: int) -> dict:
    out = {}
    for p in sorted(dir_path.glob(f"qescalate-round{n}-*.json")):
        try:
            data = json.loads(p.read_text())
            role = data.get("role") or p.stem.replace(f"qescalate-round{n}-", "")
            out[role] = data
        except json.JSONDecodeError:
            continue
    return out


def build_escalation_options(positions_round2: dict) -> list[dict]:
    """Group positions by recommendation; each group becomes a 'suggested option' for Travis."""
    groups = {}
    for role, p in positions_round2.items():
        rec = p.get("recommendation")
        groups.setdefault(rec, []).append({
            "role": role,
            "reasoning": p.get("final_reasoning", ""),
            "dissent_intensity": p.get("dissent_intensity_if_minority", "soft"),
        })
    return [
        {
            "option": rec,
            "supporters": supporters,
            "supporter_count": len(supporters),
        }
        for rec, supporters in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--council", required=True)
    ap.add_argument("--convergence", required=True)
    ap.add_argument("--round1-dir", required=True)
    ap.add_argument("--round2-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    proposal = load_json(Path(args.proposal))
    validator = load_json(Path(args.validator))
    council = load_json(Path(args.council))
    convergence = load_json(Path(args.convergence))
    round1 = load_round(Path(args.round1_dir), 1)
    round2 = load_round(Path(args.round2_dir), 2)

    # Short-circuit cases (council was skipped)
    if validator.get("vetoes_matched"):
        outcome = "veto-short-circuit"
        recommendation = "decline"
    elif validator.get("recommendation") == "act" and validator.get("confidence", 0) >= 0.7:
        outcome = "act-short-circuit"
        recommendation = "act"
    elif convergence.get("converged"):
        outcome = "converged"
        recommendation = convergence.get("majority_recommendation")
    else:
        outcome = "escalate-to-travis"
        recommendation = None

    council_decision_path = {
        "veto-short-circuit": "Validator matched veto; council was not convened.",
        "act-short-circuit": "Validator returned act with high confidence; council was not convened.",
        "converged": f"Council converged: {convergence.get('reason')}",
        "escalate-to-travis": f"Council did not converge: {convergence.get('reason')}",
    }[outcome]

    result = {
        "outcome": outcome,
        "recommendation": recommendation,
        "council_decision_path": council_decision_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proposal": proposal,
        "validator_output": validator,
        "council_composition": [
            {"role": m.get("role"), "subagent_type": m.get("subagent_type"), "persona_file": m.get("persona_file")}
            for m in council.get("council", [])
        ],
        "council_positions": {
            "round1": round1,
            "round2": round2,
        },
        "convergence": convergence,
    }

    if outcome == "escalate-to-travis":
        result["suggested_options_for_travis"] = build_escalation_options(round2)
        result["escalation_note"] = (
            "The council debated this proposal and could not converge. Below are the "
            "positions grouped by recommendation. Travis should review the dissent and pick a path. "
            "See council_positions.round2 for each member's final reasoning."
        )

    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps({
        "outcome": outcome,
        "recommendation": recommendation,
        "result_file": args.out,
    }, indent=2))


if __name__ == "__main__":
    main()
