#!/usr/bin/env python3
"""
Normalize a loose proposal input into the strict proposed-action.json shape.

Accepts:
  - A path to an existing proposed-action.json (passes through, validates required fields)
  - A path to a markdown/text file describing the proposal informally
  - Stdin with raw text

Produces a JSON object conforming to brand-and-style/decision-profile/schema/proposed-action.schema.json.

Missing fields are NOT filled by this tool — they're emitted as `null` so the
calling Claude (which has full reasoning capability) can fill them via its own
pass. The tool's job is the mechanical extraction, not the inference.

Usage:
    python3 tools/normalize-proposal.py --input <path> --out /tmp/proposal.json
    cat raw.md | python3 tools/normalize-proposal.py --out /tmp/proposal.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[2]
SCHEMA = PROJECT_ROOT / "brand-and-style" / "decision-profile" / "schema" / "proposed-action.schema.json"

REQUIRED_FIELDS = ["action", "domain", "context", "stake_estimate"]
DOMAINS = ["engineering", "sales", "client-relationship", "content", "finance",
           "personal", "strategy", "operations", "communication"]


def already_normalized(payload: dict) -> bool:
    return all(f in payload for f in REQUIRED_FIELDS) and isinstance(payload.get("stake_estimate"), dict)


def extract_field(text: str, label: str) -> str | None:
    """Pull `Label: value` or `**Label**: value` patterns from loose markdown."""
    pat = rf"(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*(.+?)(?:\n|$)"
    m = re.search(pat, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def normalize_loose(text: str) -> dict:
    out = {
        "action": extract_field(text, "Action") or extract_field(text, "Proposal") or None,
        "domain": extract_field(text, "Domain"),
        "audience": extract_field(text, "Audience"),
        "context": extract_field(text, "Context"),
        "stake_estimate": {
            "financial": extract_field(text, "Financial"),
            "time": extract_field(text, "Time"),
            "relational": extract_field(text, "Relational"),
            "reputational": extract_field(text, "Reputational"),
            "irreversibility": extract_field(text, "Irreversibility"),
        },
        "draft_payload": extract_field(text, "Draft") or extract_field(text, "Draft payload"),
        "_needs_filling": [],
    }

    if not out["action"]:
        # First non-blank line as action
        for line in text.split("\n"):
            line = line.strip().lstrip("#").strip()
            if line:
                out["action"] = line[:200]
                break

    if not out["context"]:
        out["context"] = "(loose input — context not extracted; calling agent should fill)"
        out["_needs_filling"].append("context")

    if out["domain"] and out["domain"].lower() in DOMAINS:
        out["domain"] = out["domain"].lower()
    else:
        out["_needs_filling"].append("domain")
        out["domain"] = None

    for axis, val in list(out["stake_estimate"].items()):
        if not val:
            out["stake_estimate"][axis] = None
            out["_needs_filling"].append(f"stake_estimate.{axis}")
        else:
            # Try numeric
            try:
                out["stake_estimate"][axis] = float(val)
            except (ValueError, TypeError):
                out["stake_estimate"][axis] = val.lower()

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.input:
        raw = Path(args.input).read_text()
    else:
        raw = sys.stdin.read()

    # Try parsing as JSON first
    try:
        payload = json.loads(raw)
        if already_normalized(payload):
            Path(args.out).write_text(json.dumps(payload, indent=2))
            print(f"PASS-THROUGH (already normalized): {args.out}")
            return
    except json.JSONDecodeError:
        pass

    # Loose extraction
    normalized = normalize_loose(raw)
    Path(args.out).write_text(json.dumps(normalized, indent=2))
    needs = normalized.get("_needs_filling", [])
    print(f"NORMALIZED with {len(needs)} fields needing inference: {args.out}")
    if needs:
        print(f"  Calling agent should fill: {', '.join(needs)}")


if __name__ == "__main__":
    main()
