---
tags: ["files", "espn", "sports", "architecture", "project-structure"]
category: decisions
created: 2026-02-28T16:14:35.555511
---

# GenomeUI — Key Files, ESPN and Sports Notes

# GenomeUI — Key Files, ESPN and Sports Notes

## Key Files
- `backend/auth.py` — WebAuthn passkey, session manager, OS keychain token vault
- `backend/semantics.py` — TAXONOMY, Intent definitions, extractors, classify()
- `backend/main.py` — run_operation(), ESPN fetchers, session/graph management, auth endpoints, _RateLimiter, TurnBody intent cap
- `app.js` — scene rendering, intent routing, canvas animators, passkey UI (ensureAuth), safeUrl(), safeCssColor()
- `index.css` — scene styles (computer scenes + auth overlay)
- `vite.config.js` — proxy: /api/auth → 8787, /api → 7700 (Nous), /ws → 7700; CSP headers
- `scripts/dev.ps1` — startup: Nous (required) → Backend → Frontend

## ESPN / Sports
- `_ESPN_LEAGUES`: nfl, nba, mlb, nhl, ncaaf, ncaab, ncaas (softball), ncaabase (baseball)
- Venue images fetched via ESPN venues API (not CDN guessing)
- renderDuel (with venue image) vs renderBoxScore (fallback) in app.js
- Sport keyword override in `_ext_sports`: "softball" always → ncaas regardless of team default

