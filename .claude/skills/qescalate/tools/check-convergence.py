#!/usr/bin/env python3
"""
Convergence detection on Round 2 council outputs.

Convergence rule:
  - Majority of council recommends the same action (act|draft|decline)
  - Travis-persona's recommendation must be in the majority
  - If no recommendation has a strict majority, NOT converged
  - If hard-dissent exists in the minority, recorded but does not block convergence
    (only Travis-persona-must-be-in-majority can block it)

Usage:
    python3 tools/check-convergence.py --round2-dir /tmp > /tmp/qescalate-convergence.json
"""

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path


def load_round2(round2_dir: Path) -> list[dict]:
    out = []
    for p in sorted(round2_dir.glob("qescalate-round2-*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            print(f"WARN: bad JSON in {p}", file=sys.stderr)
            continue
    return out


def detect(positions: list[dict]) -> dict:
    if not positions:
        return {"converged": False, "reason": "no positions found"}

    recs = [p.get("recommendation") for p in positions]
    counter = Counter(recs)
    most_common, count = counter.most_common(1)[0]

    n = len(positions)
    majority = count > n / 2

    travis = next((p for p in positions if "travis" in (p.get("role") or "").lower()), None)
    travis_rec = travis.get("recommendation") if travis else None
    travis_in_majority = (travis_rec == most_common) if travis else False

    dissents = [
        {
            "role": p.get("role"),
            "recommendation": p.get("recommendation"),
            "intensity": p.get("dissent_intensity_if_minority", "soft"),
            "reasoning": p.get("final_reasoning", ""),
        }
        for p in positions if p.get("recommendation") != most_common
    ]

    hard_dissents = [d for d in dissents if d["intensity"] == "hard"]

    converged = majority and travis_in_majority
    if not converged:
        if not majority:
            reason = f"no strict majority — split: {dict(counter)}"
        elif not travis_in_majority:
            reason = f"travis-persona dissents ({travis_rec}) from majority ({most_common})"
        else:
            reason = "unknown"
    else:
        reason = f"majority on '{most_common}' ({count}/{n}) with travis-persona aligned"

    return {
        "converged": converged,
        "majority_recommendation": most_common if majority else None,
        "vote_counts": dict(counter),
        "travis_persona_recommendation": travis_rec,
        "travis_in_majority": travis_in_majority,
        "reason": reason,
        "dissents": dissents,
        "hard_dissents": hard_dissents,
        "n_council_members": n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round2-dir", required=True)
    args = ap.parse_args()

    positions = load_round2(Path(args.round2_dir))
    result = detect(positions)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
