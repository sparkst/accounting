# qescalate

Universal escalation primitive. Any agent acting on Travis's behalf calls qescalate when it hits a non-trivial decision. The skill convenes a council, runs a position-debate-position cycle, and returns either a converged decision or an escalation package for Travis.

## When to call

- Validator returned `draft` and you want a council opinion before bothering Travis
- Validator returned `act` with confidence < 0.7 and meaningful stakes
- Non-trivial choice without explicit prior instruction from Travis

**Don't call**:
- Validator returned a clear `decline` (just respect it)
- Validator returned `act` with high confidence + low stakes (just act)
- Procedural / no-judgment-needed choices

## How it works

1. **Normalize** loose proposal → strict `proposed-action.json`
2. **Validate** via `decision-profile/tools/validate-proposal.py`
3. If clear `decline` or high-confidence `act`, short-circuit and return
4. **Select council** per domain — Travis-persona + skeptic + customer (if public) + 1-N domain specialists
5. **Round 1**: parallel dispatch, each member gives initial position
6. **Round 2**: each member sees others' positions, debates, gives final independent recommendation
7. **Check convergence**: majority of council on the same recommendation AND Travis-persona must be in the majority
8. **Package**: return converged decision OR escalation package with positions/dissents/options

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Operating manual the calling agent reads |
| `personas/travis-persona.md` | Travis's voice + judgment, profile-aware |
| `personas/devils-advocate.md` | Skeptic — always-on contrarian |
| `personas/customer-icp.md` | ICP-voice when audience is public/client-facing |
| `council/council-manifest.json` | Composition rules per domain |
| `tools/normalize-proposal.py` | Loose input → strict proposed-action shape |
| `tools/select-council.py` | Builds the council manifest per call |
| `tools/check-convergence.py` | Convergence detection on Round 2 outputs |
| `tools/package-result.py` | Final output: decision or escalation package |

## Convergence rule

A council has **converged** when:
- A strict majority of members recommend the same action (`act`, `draft`, or `decline`)
- AND Travis-persona's recommendation is in the majority

Otherwise → **escalate to Travis** with positions, dissent intensity, and suggested options.

This is Travis's design (interview Q for the convene skill: "majority + Travis-persona must agree"). Hard dissents from non-Travis members are surfaced in the result but don't block convergence — only Travis-persona has that power.

## Adding new advisors

To add a new persona-based advisor:
1. Write `personas/<role-name>.md` following the pattern of existing personas
2. Add the role to `council/council-manifest.json` under `always_include` or `domain_specialists.<domain>[]`

To wire an existing project subagent:
1. Find its `subagent_type` (in `.claude/agents/<name>.md`)
2. Add to `council-manifest.json` under the relevant `domain_specialists.<domain>` with a `trigger` predicate

## Limitations (v1.0)

- No semantic similarity in council selection — domain match only
- No memory across calls — each invocation is independent
- Sub-agent dispatch happens in the calling Claude Code session; not yet wrapped for non-CC callers (n8n, scripts)
- Convergence is recommendation-only; doesn't yet check reasoning consistency
- ICP-voice picks an archetype heuristically; future: explicit archetype selection per call

## Testing

Run the skill against a sample proposal:

```bash
# Compose a proposal
cat > /tmp/test-proposal.json <<'EOF'
{
  "action": "Draft a LinkedIn article on AI maturity tiers for solo operators",
  "domain": "content",
  "audience": "public",
  "context": "From transcript-mining article seeds, high-value",
  "stake_estimate": {"financial": 0, "time": "low", "relational": "low", "reputational": "medium", "irreversibility": "sticky"}
}
EOF

# Invoke the skill from inside Claude Code: /qescalate /tmp/test-proposal.json
```

The skill orchestrator (you, the calling Claude) reads `SKILL.md` and runs the steps. Intermediate state lands in `/tmp/qescalate-*.json`. Final result at `/tmp/qescalate-result.json`.
