---
tags: ["genomeui", "build-history", "architecture", "issues", "codex-chat"]
category: decisions
created: 2026-02-21T12:25:29.725350
---

# GenomeUI Build History & Current State

# GenomeUI Build History & Current State

## Architecture
- **Backend**: Python/FastAPI in `.venv` → `backend/main.py`, port 8787
- **Frontend**: Vite + vanilla JS → `app.js`, `index.css`, `index.html`, port 5173
- **Realtime sync**: WebSocket primary → SSE fallback → polling
- **Intent kernel**: 4-layer intent envelope (surface, task, state, ui)
- **Session store**: in-memory + SQLite persistence
- **Graph**: entity/relation graph with constraint enforcement

## What Was Built (T1–T142+)
- **T1–T8**: Capability kernel, policy gating, audit journal, kernel path coverage guard, replay smoke tests
- **T9–T24**: World model graph (typed entities, relations, depends_on, cycle detection, transitive chains)
- **T25–T43**: Handoff continuity, presence heartbeats, idempotency, conflict resolution (409), merge rebase
- **T44–T65**: Continuity autopilot (enable/disable/tick), guardrails, mode profiles (safe/normal/aggressive), rate limits
- **T66–T142**: Full continuity observability stack: health, alerts, anomalies, incidents, metrics, posture, forecast, guidance, remediation, matrix views
- **T143–T145+**: Graph schema/query/neighborhood/pathfinding APIs
- **Connectors**: Shopping intent, weather intent, live Puma scraping, webdeck surface

## Where It Broke Down (Last ~200 lines of chat)
The user's core frustration: **the UI still looks like a dashboard, not an immersive generative OS**

### Shopping/Web Intent Issues
- Stock images (Unsplash placeholders) were showing instead of real product images
- "Browserless browser" webdeck was demanded: browser-chrome-like shell, immersive, visual-first
- Codex claimed live Puma scraping worked but UI wasn't reflecting changes (stale runtime)
- User wants: specific brand query → go directly to that brand, not show scaffolded data

### Visual Philosophy (what user wants)
- **Every intent must be equally visual AND textual** — not a dashboard
- Weather → full-bleed image/radar, not charts
- Shopping → real product images from real sources OR direct brand routing
- Tasks/notes/expenses → visual lane boards, masonry tiles, spend bars (not tables)
- Global rule: main canvas = visuals, right rail = compact status only
- "A browser without being a browser" — immersive webdeck for web intents

### Current Uncommitted State
The last GitHub commit is `b6499bf` ("Build OS runtime and stabilize test harness through T142"). Files modified since then per git status:
- `app.js`, `backend/main.py`, `index.css`, `README.md`
- `scripts/replay_smoke.py`, `tests/ui/operator.spec.js`, `tests/ui/trace.spec.js`, `tests/ui/undo.spec.js`
- New untracked: `docs/CONNECTOR_ADAPTER_CONTRACTS.md`, `docs/CONNECTOR_SPEC_V1.md`, `scripts/connector_replay.py`, `tests/unit/`

## Key Test Commands
- `npm run os:test:replay` — backend replay smoke
- `npm run os:test:handoff` — handoff stress
- `npm run ui:test` — Playwright (5 specs)
- `npm run verify:all` — replay + slo + ui + build
- `python -m unittest tests.unit.test_connectors`

## Key Files
- `backend/main.py` — entire OS backend (very large, ~10k+ lines)
- `app.js` — frontend intent compiler, surface renderer
- `index.css` — full-screen OS shell styling
- `scripts/replay_smoke.py` — backend E2E replay
- `tests/ui/` — Playwright specs

## Outstanding Functional Problems
1. Shopping/web intent: real data vs mock, webdeck implementation
2. Visual rendering: immersive vs dashboard
3. Connector live data pipeline (Puma scraping reliability)
4. Changes not reflecting due to stale runtime (Ctrl+F5 workaround)

