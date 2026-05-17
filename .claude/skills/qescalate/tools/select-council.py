#!/usr/bin/env python3
"""
Build the council manifest for a given proposal.

Reads:
  - proposed-action.json
  - validator output
  - council-manifest.json (the composition rules)

Writes:
  - council manifest with selected roles + which Agent invocations to make

Usage:
    python3 tools/select-council.py --proposal /tmp/qescalate-proposal.json \
        --validator /tmp/qescalate-validation.json > /tmp/qescalate-council.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COUNCIL_RULES = ROOT / "council" / "council-manifest.json"
PROJECT_ROOT = ROOT.parents[2]


def load_json(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def trigger_matches(trigger: str, proposal: dict) -> bool:
    if trigger == "any":
        return True
    text = (
        proposal.get("action", "") + " " +
        proposal.get("context", "") + " " +
        proposal.get("draft_payload", "")
    ).lower()

    triggers_map = {
        "draft_payload contains code": bool(re.search(r"(import\s+\w|from\s+\w+\s+import|def\s+\w|function\s+\w|class\s+\w)", proposal.get("draft_payload", ""))),
        "contains factual claims": bool(re.search(r"\b(\d+%|study|research shows|according to|data shows)\b", text)),
        "outbound to external party": proposal.get("audience") in ("public", "client-facing"),
        "involves pricing or financial commitment": bool(re.search(r"\b(price|fee|cost|invoice|payment|retainer|\$)\b", text)),
        "involves contract or IP": bool(re.search(r"\b(contract|agreement|nda|ip\s+retention|license|terms)\b", text)),
        "architecture or pattern question": bool(re.search(r"\b(architecture|pattern|abstraction|refactor|design)\b", text)),
        "involves a deliverable": bool(re.search(r"\b(deliverable|artifact|document|report|deck|presentation)\b", text)),
        "involves tooling or process changes": bool(re.search(r"\b(tool|script|process|workflow|pipeline|cron)\b", text)),
    }
    return triggers_map.get(trigger, False)


def build_council(proposal: dict, rules: dict) -> list[dict]:
    council = []
    council.extend(rules.get("always_include", []))

    audience_rule = rules.get("include_when_audience_in", {})
    if proposal.get("audience") in audience_rule.get("audience_values", []):
        council.extend(audience_rule.get("members", []))

    domain = proposal.get("domain", "")
    specialists = rules.get("domain_specialists", {}).get(domain, [])
    matched_specialists = [s for s in specialists if trigger_matches(s.get("trigger", "any"), proposal)]
    council.extend(matched_specialists)

    # Cap at max_council_size, preserving always-include + the most-specific specialists
    max_size = rules.get("max_council_size", 6)
    if len(council) > max_size:
        always = [c for c in council if c.get("required_for_convergence") or c.get("role") == "skeptic" or c.get("role") == "customer"]
        rest = [c for c in council if c not in always]
        council = always + rest[: max_size - len(always)]

    return council


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--validator")
    args = ap.parse_args()

    proposal = load_json(Path(args.proposal))
    rules = load_json(COUNCIL_RULES)

    council = build_council(proposal, rules)

    output = {
        "proposal_summary": proposal.get("action", "")[:120],
        "domain": proposal.get("domain"),
        "audience": proposal.get("audience"),
        "council_size": len(council),
        "council": council,
        "convergence_rule": "majority on recommendation AND travis-persona must be in the majority",
        "min_council_size_met": len(council) >= rules.get("min_council_size", 3),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
