#!/bin/bash
# Deploy accounting dashboard locally — builds, installs LaunchAgents, starts services
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
UID_NUM=$(id -u)

echo "=== Accounting Local Deploy ==="
echo "Repo: $REPO_DIR"

# 1. Build dashboard
echo ""
echo "[1/4] Building dashboard..."
cd "$REPO_DIR/dashboard"
npm run build

# 2. Copy LaunchAgent plists
echo ""
echo "[2/4] Installing LaunchAgents..."
mkdir -p "$PLIST_DIR"
for plist in com.sparkry.accounting-api com.sparkry.accounting-dashboard com.sparkry.caddy-accounting; do
    cp "$REPO_DIR/$plist.plist" "$PLIST_DIR/"
    echo "  Installed $plist"
done

# 3. Stop existing services
echo ""
echo "[3/4] Restarting services..."
for svc in com.sparkry.accounting-api com.sparkry.accounting-dashboard com.sparkry.caddy-accounting; do
    launchctl bootout "gui/$UID_NUM/$svc" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$PLIST_DIR/$svc.plist" 2>/dev/null || true
    echo "  Started $svc"
done

# 4. Verify
echo ""
echo "[4/4] Verifying..."
sleep 3
curl -sf http://127.0.0.1:8000/api/health > /dev/null && echo "  API: UP" || echo "  API: DOWN"
curl -sf http://127.0.0.1:5173 > /dev/null && echo "  Dashboard: UP" || echo "  Dashboard: DOWN"

echo ""
echo "=== Deploy complete ==="
echo "Dashboard: https://macbook.ancon-cliff.ts.net"
echo "API:       https://macbook.ancon-cliff.ts.net/api/"
