---
tags: ["scene", "rendering", "canvas", "integrations", "connectors", "oauth"]
category: decisions
created: 2026-02-28T16:13:31.761313
---

# GenomeUI — Scene Rendering System & Integration Layer

# GenomeUI — Scene Rendering System & Integration Layer

## Scene / Rendering System
- Every intent maps to a scene type rendered on the canvas
- Scenes grow with the taxonomy — new intent domains need new scene renderers
- Canvas-based background (team colors, weather gradients, etc.) + HTML overlay content
- `activateSceneGraphics()` in app.js maps scene type → canvas renderer
- Computer scenes (document/spreadsheet/presentation/code/terminal/calendar/email/content) all use `makeComputerRenderer(canvas)` — reads `canvas.dataset.scene` for per-type decoration

## Integration Layer (next major work)
- Auth infrastructure is in place — OAuth token vault ready for connectors
- Integration pattern: intent → connector → API call → data normalized → scene rendered
- Auth flow: OAuth → `vault_store(service, tokens)` → stored in OS keychain
- Priority connectors: Spotify, Gmail, Google Calendar, Google Drive, Slack, Plaid
- Each service needs: OAuth flow handler in backend + scene renderer in app.js
- The experience IS the product — scenes should feel like GenomeUI, not like the source app

