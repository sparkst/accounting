# Planning Engine — Operator Notes

Sub-project #1 of the Sparks Retirement & Business Sustainability Model
integration. See `docs/superpowers/specs/2026-06-01-planning-engine-design.md`
for the design.

## Monthly scheduled job

`com.sparkry.planning-monthly.plist` runs on the 1st of each month at 06:00.

### Install

```bash
cp com.sparkry.planning-monthly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
launchctl list | grep planning   # confirm loaded
```

### Run on demand

```bash
launchctl start com.sparkry.planning-monthly
# Or skip launchd and run directly:
doppler run -- python -m src.planning simulate
```

### Inspect logs

```bash
tail -f ~/Library/Logs/com.sparkry.planning-monthly.log
tail -f ~/Library/Logs/com.sparkry.planning-monthly.error.log
```

### Reload after code change

```bash
launchctl unload ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
launchctl load ~/Library/LaunchAgents/com.sparkry.planning-monthly.plist
```

## Ad-hoc CLI

```bash
doppler run -- python -m src.planning simulate                        # full run, persist
doppler run -- python -m src.planning simulate --dry-run              # no persist
doppler run -- python -m src.planning simulate --override spend_start=300000 --note "what if?"
doppler run -- python -m src.planning simulate --scenarios baseline_ret8_horizon85
doppler run -- python -m src.planning show-latest
doppler run -- python -m src.planning compare --since 2026-01-01
```

## API

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/api/planning/runs/latest
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: No AccountBalanceSnapshot rows` | Plaid balance sync hasn't run | `doppler run -- python scripts/plaid_balance_sync.py` first |
| `WARNING: latest AccountBalanceSnapshot is N days old` | Plaid balance sync stale | Same — re-run sync, then re-invoke planning |
| `unknown override key: X` | Typo in `--override KEY=val` | CLI lists valid keys in the error |
