---
tags: ["security", "hardening", "auth", "rate-limiting", "injection", "fixes"]
category: decisions
created: 2026-02-28T16:10:26.645020
---

# Security Hardening — All Gaps Resolved (Feb 2026)

# Security Hardening — GenomeUI (Feb 2026)

All known security gaps have been patched across multiple commits. Summary below.

## Commits
- `dee0561` — Patch six security vulnerabilities
- `fdde16f` — Harden remaining gaps: WS auth, CSP, slot sanitization, startup warning
- `d953f40` — Fix CSS injection via unvalidated block.color
- `dbf487c` — Block javascript: URL injection in all href/src attributes
- `ad32aae` — Close iframe sandbox escape, unbounded intent, rate limiting

## Fixes Applied

### Network / CORS
- CORS restricted to explicit localhost origins (was `["*"]`)

### Auth & Sessions
- Session IDs: `secrets.token_urlsafe(16)` — 128-bit entropy (was UUID[:8])
- `/api/turn` HTTP endpoint gated by `X-Genome-Auth` header check
- `/api/connectors/secrets` endpoints require valid auth token
- WebSocket auth: token sent as first JSON message `{type:"auth",token,sessionId}` — no longer in URL query params

### Secrets / Vault
- Connector vault: AES-256-GCM with key in OS keychain (was XOR + hardcoded key)

### Input Validation
- `nousIntent.op` validated against `CAPABILITY_REGISTRY` whitelist before use
- Nous `slots` sanitized via `_sanitize_slots()` — only primitives, strings capped at 512 chars
- `TurnBody.intent` capped at 2000 chars via Pydantic `Field(max_length=2000)`

### Injection Prevention
- `safeCssColor()` validates all dynamic CSS color values (blocks injection via block.color)
- `safeUrl()` validates all href/src attributes — rejects `javascript:` and non-http(s)/mailto/tel schemes; calls `escapeAttr()` on result

### iframe
- Removed `allow-same-origin` from iframe sandbox (combined with `allow-scripts` = sandbox escape vector)

### Rate Limiting
- `_RateLimiter` token-bucket class (stdlib, no new deps) in `backend/main.py`
- 60 req/min on `/api/turn`, 10 req/min on `/api/auth/register/begin` and `/api/auth/login/begin`
- Keyed by client IP; returns HTTP 429 on breach

### Frontend Security
- CSP headers added to Vite dev server: `script-src self; style unsafe-inline; img-src self espn.com; connect-src localhost ports`

## Key Files
- `backend/main.py` — rate limiter, intent cap, op whitelist, auth checks
- `backend/auth.py` — session IDs, WebSocket auth, vault encryption
- `app.js` — safeUrl(), safeCssColor(), iframe sandbox fix
- `vite.config.js` — CSP headers, CORS origins

