---
name: qescalate
description: Universal escalation primitive — any agent that needs Travis's judgment calls qescalate. Convenes a council (Travis-persona + skeptic + customer-voice + domain SMEs), runs a position-debate-position cycle, attempts convergence via majority + Travis must agree, returns either a converged decision or an escalation package for Travis. Use whenever a running agent has a non-trivial proposal that the decision-profile validator returned as `draft` OR ambiguous, and the caller wants a sub-team judgment before bothering Travis.
---

# qescalate — Council-of-Advisors Escalation Primitive

## What this skill is

When a calling agent (any Claude Code skill, subagent, or human-in-the-loop session) hits a decision it can't confidently make on its own, it invokes qescalate. The skill:

1. **Normalizes** the loose proposal into the strict `proposed-action.json` shape (LLM-assisted stake estimation)
2. **Validates** via `decision-profile/tools/validate-proposal.py` — if veto or clear `decline`/`act`, returns immediately (no need for council)
3. **Selects** a council per domain (see `council-manifest.json`):
   - **Travis-persona** (always)
   - **Skeptic / Devil's advocate** (always)
   - **Customer / ICP-voice** (when domain ∈ content/sales/communication or audience ∈ public/client-facing)
   - **1–N existing project subagents** matched to the situation (finance-consultant, legal-expert, security-reviewer, strategic-advisor, code-quality-auditor, ux-tester, etc.)
4. **Round 1 — initial positions**: dispatches the council in parallel, each returns position + reasoning + recommendation
5. **Round 2 — debate**: bundles round-1 outputs, dispatches each member again with all others' positions; each can update and emits a **final independent recommendation**
6. **Checks convergence**: majority of council on the same recommendation **AND Travis-persona's recommendation must be in the majority**
7. **Returns**:
   - **Converged**: structured decision + full reasoning trace
   - **Not converged**: escalation package — positions, dissents, suggested options for Travis

The caller is responsible for **acting on the result** (the skill never sends emails / pushes code / takes external action). qescalate is a judgment primitive.

## When to invoke

✅ **Yes**:
- Validator returned `draft` and you want a council's opinion before sending it to Travis
- Validator returned `act` but confidence < 0.7 and stakes are above the escalation floor
- You're an agent acting on Travis's behalf, you have a non-trivial choice, and you don't have explicit instructions for this case
- A debate would clearly help (multiple plausible paths)

❌ **No**:
- Validator returned a clear `decline` — just return that (council won't override a veto)
- Validator returned `act` with high confidence and stakes below threshold — just act
- The decision is procedural / no judgment needed (file naming, format choices, etc.)
- Travis already gave explicit per-instance authorization in conversation

## Invocation pattern

Inside another agent's flow:

```
1. Run validator: python3 brand-and-style/decision-profile/tools/validate-proposal.py --input proposal.json
2. If recommendation requires escalation, invoke this skill: /qescalate <proposal-path>
3. Skill returns structured JSON; act on it
```

From a non-Claude-Code caller (n8n, cron, script): not directly supported in v1.0 — caller must spawn a Claude Code session. (Future: HTTP wrapper.)

## Workflow detail (read this when running the skill)

You are the orchestrator of this council. Travis is not in the room — the Travis-persona advisor speaks for him in the council; you don't make decisions on Travis's behalf, you convene + facilitate + report.

### Step 1 — Normalize proposal

```bash
python3 .claude/skills/qescalate/tools/normalize-proposal.py --input <path-or-stdin> --out /tmp/qescalate-proposal.json
```

If the input is already a valid `proposed-action.json`, the script returns it unchanged. Otherwise, the script asks you (the calling Claude) to fill missing fields by reading the proposal and inferring `domain`, `audience`, `stake_estimate`. Run an LLM pass via direct reasoning — do NOT prompt the user for missing fields; the council's job is to gather missing context.

### Step 2 — Validate

```bash
python3 brand-and-style/decision-profile/tools/validate-proposal.py --input /tmp/qescalate-proposal.json > /tmp/qescalate-validation.json
```

Read the output. If `recommendation` is `decline` AND `vetoes_matched` is non-empty → **return validator output as final result; no council**. If `recommendation` is `act` AND confidence ≥ 0.7 → **return validator output as final result; no council**. Otherwise → continue to council.

### Step 3 — Select council

```bash
python3 .claude/skills/qescalate/tools/select-council.py --proposal /tmp/qescalate-proposal.json --validator /tmp/qescalate-validation.json > /tmp/qescalate-council.json
```

This outputs a manifest:
```json
{
  "council": [
    {"role": "travis-persona", "persona_file": ".claude/skills/qescalate/personas/travis-persona.md", "domain": "any"},
    {"role": "skeptic",        "persona_file": ".claude/skills/qescalate/personas/devils-advocate.md", "domain": "any"},
    {"role": "customer",       "persona_file": ".claude/skills/qescalate/personas/customer-icp.md", "domain": "content"},
    {"role": "domain-sme",     "subagent_type": "finance-consultant", "domain": "finance"},
    ...
  ]
}
```

### Step 4 — Round 1 (initial positions)

For each council member in the manifest, dispatch an Agent in parallel. Each agent prompt is:

```
You are a council member helping Travis Sparks's delegated agent reach a judgment on a proposed action.

YOUR ROLE: <role> (persona file: <persona_file>, or subagent_type: <subagent_type>)

Read your persona file fully. Read the proposal at /tmp/qescalate-proposal.json. Read the validator output at /tmp/qescalate-validation.json.

Also read: brand-and-style/decision-profile/profile.json (you may need to look at this — particularly the principles, heuristics, and vetoes — to reason like Travis would.)

Your task — Round 1: give your INITIAL position on this proposal. Independent reasoning, no awareness of what other council members think yet.

Output exactly this JSON to /tmp/qescalate-round1-<role>.json:
{
  "role": "<your role>",
  "recommendation": "act" | "draft" | "decline",
  "key_reasoning": "<2-4 sentences>",
  "concerns": ["<concern 1>", "<concern 2>"],
  "principles_invoked": ["P-NNN", ...],   // when applicable
  "dissent_intensity_if_minority": "soft" | "hard"   // how strongly you'd push back if you ended up in the minority
}

Reply with: WROTE /tmp/qescalate-round1-<role>.json
```

### Step 5 — Round 2 (debate + final positions)

Collect all round-1 outputs. Dispatch each council member again, in parallel, with:

```
You are <role>. This is Round 2 of the council debate.

Read your Round 1 output at /tmp/qescalate-round1-<role>.json. Read all other council members' Round 1 outputs (you'll see them in /tmp/qescalate-round1-*.json).

Now consider their arguments. You may update your position OR hold it. Do NOT just average — be honest about whether anyone changed your mind, and why or why not. The council values vocal dissent over false agreement (P-005, P-008).

Output to /tmp/qescalate-round2-<role>.json:
{
  "role": "<role>",
  "recommendation": "act" | "draft" | "decline",
  "final_reasoning": "<2-4 sentences>",
  "position_changed": true | false,
  "what_changed_my_mind": "<if changed: name the argument; else empty>",
  "concerns": ["..."],
  "dissent_intensity_if_minority": "soft" | "hard"
}

Reply with: WROTE /tmp/qescalate-round2-<role>.json
```

### Step 6 — Check convergence

```bash
python3 .claude/skills/qescalate/tools/check-convergence.py --round2-dir /tmp > /tmp/qescalate-convergence.json
```

Convergence rule (per Travis's design):
- **Converged**: majority of council on the same `recommendation` AND Travis-persona's recommendation is in the majority
- **Not converged**: split, OR Travis-persona is the minority

### Step 7 — Package result

```bash
python3 .claude/skills/qescalate/tools/package-result.py \
  --proposal /tmp/qescalate-proposal.json \
  --validator /tmp/qescalate-validation.json \
  --council /tmp/qescalate-council.json \
  --convergence /tmp/qescalate-convergence.json \
  --round1-dir /tmp \
  --round2-dir /tmp \
  --out /tmp/qescalate-result.json
```

The result JSON has the structure:

```json
{
  "outcome": "converged" | "escalate-to-travis",
  "recommendation": "act" | "draft" | "decline" | null,
  "council_decision_path": "majority + travis agreed" | "no convergence" | "veto-short-circuit" | "act-short-circuit",
  "validator_output": { ... },
  "council_positions": {
    "round1": { "role": position-dict, ... },
    "round2": { "role": position-dict, ... }
  },
  "dissents": [{ "role": "...", "recommendation": "...", "intensity": "hard|soft", "reasoning": "..." }],
  "suggested_options_for_travis": [...]   // populated when escalating
}
```

## Output to the caller

Print the result JSON to stdout. The calling agent reads it and decides what to do (act, draft for Travis, or escalate).

## Reminders for you, the orchestrator

- Always read each persona file BEFORE dispatching — the persona is the agent's identity in this council
- Dispatch round-1 in parallel (single message, multiple Agent calls), same for round-2
- The Travis-persona is the most important council member — its recommendation is required for convergence
- If you encounter a veto in Step 2, do not run the council; vetoes are not debatable
- Save intermediate state in `/tmp/qescalate-*.json` so a re-run can pick up where you left off
- A debate that ends in escalation is a successful run, not a failure — the council surfaced the dissent that Travis needs to see
