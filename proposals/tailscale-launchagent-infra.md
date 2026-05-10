# Proposal: Tailscale Exposure + LaunchAgent Auto-Start

**Author:** Quark (CFO) + LaForge (Engineering, consulted)
**To:** Picard (CEO)
**Date:** 2026-03-26
**Status:** Draft — awaiting LaForge input on approach

---

## Problem

1. **Dashboard only accessible from Mac browser** — Travis can't check financials from his phone. The "Travis Standard" requires mobile access via Tailscale.
2. **Manual server restart** — If the Mac reboots, both servers (API + dashboard) go down until someone runs terminal commands. Not acceptable for a system Travis depends on daily.

## Proposed Solution

### 1. Tailscale Exposure

Expose the accounting dashboard and API via the Mac's Tailscale address so Travis can access from any device on his tailnet (phone, iPad, other machines).

**Option A: Caddy Reverse Proxy** (recommended if we need HTTPS)
- Caddy listens on Tailscale interface port 443
- Reverse proxies to localhost:5173 (dashboard) and localhost:8000 (API)
- Automatic HTTPS via Tailscale cert integration
- URL: `https://macbook.ancon-cliff.ts.net/accounting/`

**Option B: Direct Tailscale Binding** (simpler)
- Change uvicorn to bind `0.0.0.0:8000` instead of `127.0.0.1:8000`
- Change SvelteKit to bind `0.0.0.0:5173`
- Tailscale ACLs restrict access to Travis's devices only
- URL: `http://macbook.ancon-cliff.ts.net:5173/`

**Security:** API key auth is already implemented (Sprint 1). Tailscale provides network-level encryption and device authentication. Combined: Tailscale ACL + API key = sufficient for a personal accounting system.

### 2. LaunchAgent Auto-Start

Two new LaunchAgents (matching the existing backup LaunchAgent pattern):

**com.sparkry.accounting-api.plist**
```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/travis/SGDrive/dev/accounting/.venv/bin/uvicorn</string>
  <string>src.api.main:app</string>
  <string>--host</string>
  <string>0.0.0.0</string>
  <string>--port</string>
  <string>8000</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/travis/SGDrive/dev/accounting</string>
<key>RunAtLoad</key>
<true/>
<key>KeepAlive</key>
<true/>
```

**com.sparkry.accounting-dashboard.plist**
```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/node</string>
  <string>/Users/travis/SGDrive/dev/accounting/dashboard/node_modules/.bin/vite</string>
  <string>preview</string>
  <string>--host</string>
  <string>0.0.0.0</string>
  <string>--port</string>
  <string>5173</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/travis/SGDrive/dev/accounting/dashboard</string>
<key>RunAtLoad</key>
<true/>
<key>KeepAlive</key>
<true/>
```

**Note:** Using `vite preview` (production build) instead of `npm run dev` for:
- Faster startup, lower memory usage
- No HMR overhead
- Stable for unattended operation
- Requires `npm run build` before first use (and after each code update)

### 3. Build-on-Deploy Script

`scripts/deploy-local.sh` — runs build + restarts LaunchAgents:
```bash
#!/bin/bash
cd ~/SGDrive/dev/accounting/dashboard
npm run build
launchctl kickstart -k gui/$(id -u)/com.sparkry.accounting-dashboard
launchctl kickstart -k gui/$(id -u)/com.sparkry.accounting-api
```

## Scope

| Task | Effort | Owner |
|------|--------|-------|
| Create API LaunchAgent plist | S | Quark |
| Create Dashboard LaunchAgent plist | S | Quark |
| Create deploy-local.sh script | S | Quark |
| Configure Tailscale binding (Option A or B) | M | LaForge |
| Test phone access | S | Travis |

**Total: 1-2 hours.** No QRALPH needed — this is infrastructure config, not application code.

## Open Questions (for LaForge)

1. Caddy (Option A) vs direct binding (Option B)?
2. Should dashboard use `vite preview` or keep `npm run dev`?
3. Any Tailscale ACL changes needed?

---

*Awaiting LaForge's input to finalize approach. — Quark*
