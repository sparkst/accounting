# Plaid OAuth-Return Cloudflare Tunnel

**Status:** Phase 1 prerequisite — not optional. Without this tunnel, 7 of the 10
planned Plaid Items (Chase, BofA, Vanguard, Fidelity, Schwab, Citi, plus any
OAuth-only investments) will fail at link time. Tailscale-only domains are not
reachable by Plaid's infrastructure.

## What's exposed

**Only** `https://accounting-plaid.<your-subdomain>/admin/connections/oauth-return`.
Everything else in the dashboard (the rest of `/admin/*`, transactions, brokerage,
tax pages) stays Tailscale-only — Caddy at `macbook.ancon-cliff.ts.net` continues
to front them on the tailnet.

The OAuth-return page itself is trivial: it `postMessage`s back to the opener
window and renders a "you can close this tab" message. It does not query the API
or load secrets — Plaid Link finishes the flow inside the original window via
the existing API-key-authenticated endpoints.

## Setup steps

### 1. Pick a subdomain you control

You need a public hostname Plaid can reach. Use a Cloudflare-managed zone you
already own. For the rest of this doc we'll use `plaid.example.com` as the
placeholder — substitute your real hostname.

### 2. Install `cloudflared`

```bash
brew install cloudflared
cloudflared --version  # >= 2024.x
```

### 3. Authenticate and create the tunnel

```bash
cloudflared tunnel login                              # opens browser; pick the zone
cloudflared tunnel create plaid-oauth-return         # save the UUID it prints
```

### 4. Configure the tunnel to expose only the OAuth-return path

Create `~/.cloudflared/config.yml` (or update an existing one):

```yaml
tunnel: <UUID from previous step>
credentials-file: /Users/travis/.cloudflared/<UUID>.json

ingress:
  # Match only the OAuth-return path. Everything else 404s — keeps the rest of
  # the dashboard hidden from the public internet.
  - hostname: plaid.example.com
    path: /admin/connections/oauth-return
    service: http://127.0.0.1:5173
  - hostname: plaid.example.com
    # Catch-all for the configured hostname; return 404.
    service: http_status:404
  # Required terminating catch-all.
  - service: http_status:404
```

### 5. Route DNS

```bash
cloudflared tunnel route dns plaid-oauth-return plaid.example.com
```

This creates the CNAME pointing `plaid.example.com` at the tunnel.

### 6. Run the tunnel as a service

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

Logs land in `/Library/Logs/com.cloudflare.cloudflared.log`.

### 7. Verify

```bash
curl -I https://plaid.example.com/admin/connections/oauth-return
# Should return 200 (or 304 from your dashboard's HTML cache).

curl -I https://plaid.example.com/admin/connections
# Should return 404. This is intentional — only the oauth-return path is public.

curl -I https://plaid.example.com/api/transactions
# Should return 404. The full API stays Tailscale-gated.
```

### 8. Register the URL in the Plaid Dashboard

1. Go to https://dashboard.plaid.com/team/api.
2. Under "Allowed redirect URIs", add:
   ```
   https://plaid.example.com/admin/connections/oauth-return
   ```
3. Save.

This must be registered BEFORE any OAuth-bank link attempt. Plaid refuses
mismatched redirect_uris.

## Operational notes

- The dashboard at port 5173 is served by `vite preview` via
  `com.sparkry.accounting-dashboard.plist`. If you run `npm run dev` instead
  (port still 5173), the tunnel works the same.
- The link-token is requested with the redirect_uri matching the public URL.
  When Phase 1 sandbox testing flips to production, the redirect_uri **must**
  also be registered against your production Plaid application — sandbox and
  production maintain separate allow-lists.
- To tear it all down (slot-recovery scenario):
  ```bash
  sudo launchctl stop com.cloudflare.cloudflared
  cloudflared tunnel delete plaid-oauth-return
  ```
  Doing so will break any in-flight OAuth-bank links until the tunnel is
  recreated.

## Why not just expose the whole dashboard?

The dashboard is auth-gated by API key, but defense-in-depth matters: a leaked
key would expose the entire accounting register to the public internet. Limiting
the tunnel to one inert HTML page means the blast radius of a Cloudflare-side
compromise is "an attacker sees a page that says 'authorization complete'."
