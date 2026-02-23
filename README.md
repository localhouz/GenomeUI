# Generative UI (The "No-App" OS)

This is a functional prototype of a **Latent Surface** with layered intent routing, real-time cross-device sync, and local-first planning.

See `BUILDPLAN.md` for the full Generative OS execution roadmap.
Current sprint execution board: `docs/TASKBOARD.md`.

## 🚀 Getting Started (Venv-First)

1. **Create Python virtual environment**:
   ```powershell
   C:\Users\steve\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv
   ```

2. **Install Python backend dependencies**:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. **Install frontend dependencies**:
   ```bash
   npm install
   ```

4. **Optional: Enable local LLM planning (Ollama)**:
   ```bash
   copy .env.example .env
   # set OLLAMA_MODEL_SMALL and/or OLLAMA_MODEL_LARGE
   # leave both empty for deterministic local-only mode
   ```

5. **Run full stack**:
   ```bash
   npm run dev
   ```
   Or use the single OS launcher (prints desktop + phone URLs):
   ```bash
   npm run dev:os
   ```
   First-time bootstrap + run:
   ```bash
   npm run dev:os:bootstrap
   ```
   Backend smoke test only:
   ```bash
   npm run os:test
   ```
   Connector/unit test suite:
   ```bash
   npm run os:test:unit
   ```
   Deterministic connector replay suite (offline/mock provider):
   ```bash
   npm run os:test:connectors
   ```
   Mutation replay smoke test:
   ```bash
   npm run os:test:replay
   ```
   SLO throttle stress test:
   ```bash
   npm run os:test:slo
   ```
   Handoff continuity stress test:
   ```bash
   npm run os:test:handoff
   ```
   UI regression suite (Playwright, serial worker for stability):
   ```bash
   npm run ui:test
   ```
   Full verification (replay + UI + build):
   ```bash
   npm run verify:all
   ```

6. **Open**: `http://localhost:5173`

## CI / Release
- CI workflow: `.github/workflows/ci.yml`
  - Linux validate job: build + Python unit tests + Electron module tests + Playwright smoke
  - Windows package job: `electron:build:local` + installer artifact upload
  - Tauri compile job: `cargo check --manifest-path src-tauri/Cargo.toml`
- Manual release workflow: `.github/workflows/release.yml`
  - `workflow_dispatch` builds Windows installer artifact from selected ref
- Local CI parity command:
  ```bash
  npm run ci:local
  ```

## 📱 Cross-Device Continuity
Use the same session URL on desktop + phone:
- Desktop: `http://localhost:5173/?session=mysharedsurface`
- Phone (same Wi-Fi): `http://<your-lan-ip>:5173/?session=mysharedsurface`

Realtime sync transport priority:
1. WebSocket (`/ws`)
2. SSE (`/api/stream?sessionId=<id>`)
3. Polling fallback

## 🖥 Backend (Python / venv)
FastAPI service in `backend/main.py` provides:
- `POST /api/session/init`
- `GET /api/connectors`
- `GET /api/connectors/providers`
- `GET /api/connectors/grants`
- `POST /api/connectors/grants`
- `GET /api/connectors/secrets`
- `POST /api/connectors/secrets`
- `GET /api/connectors/mock/weather?location=Seattle`
- `GET /api/connectors/mock/banking?limit=5`
- `GET /api/connectors/mock/social`
- `GET /api/connectors/mock/web?url=https://example.com`
- `GET /api/connectors/mock/contacts?query=mike`
- `GET /api/session/{sessionId}`
- `GET /api/session/{sessionId}/presence`
- `POST /api/session/{sessionId}/presence`
- `POST /api/session/{sessionId}/presence/prune`
- `GET /api/session/{sessionId}/journal?limit=50`
- `GET /api/session/{sessionId}/audit?domain=&risk=&ok=&op=&policy_code=&limit=&format=json|ndjson`
- `GET /api/session/{sessionId}/trace?limit=50&ok=&intent_class=&route_reason=&format=json|ndjson`
- `GET /api/session/{sessionId}/trace/summary?limit=200`
- `GET /api/session/{sessionId}/graph?limit=200`
- `GET /api/session/{sessionId}/graph/schema`
- `GET /api/session/{sessionId}/graph/health`
- `GET /api/session/{sessionId}/graph/components?relation=depends_on&limit=20`
- `GET /api/session/{sessionId}/graph/hubs?relation=depends_on&limit=10`
- `GET /api/session/{sessionId}/graph/events?kind=link_entities&limit=20`
- `GET /api/session/{sessionId}/graph/summary?relation=depends_on&limit=10`
- `GET /api/session/{sessionId}/graph/relation-matrix?relation=depends_on&limit=100`
- `GET /api/session/{sessionId}/graph/anomalies?limit=20`
- `GET /api/session/{sessionId}/graph/guidance?limit=8`
- `GET /api/session/{sessionId}/graph/score`
- `GET /api/session/{sessionId}/graph/score-trend?window_ms=3600000&buckets=8`
- `GET /api/session/{sessionId}/graph/score-guidance?limit=6`
- `GET /api/session/{sessionId}/graph/score-alerts?limit=10`
- `GET /api/session/{sessionId}/graph/score-alerts-history?window_ms=3600000&buckets=8&limit=5`
- `GET /api/session/{sessionId}/graph/score-remediation?limit=6`
- `GET /api/session/{sessionId}/graph/score-forecast?horizon_ms=3600000&step_buckets=6`
- `GET /api/session/{sessionId}/graph/score-forecast-guidance?limit=6`
- `GET /api/session/{sessionId}/graph/score-guardrails?warn_below=75&fail_below=60`
- `GET /api/session/{sessionId}/graph/score-autopilot-preview?limit=6`
- `POST /api/session/{sessionId}/graph/score-autopilot/run` with `{ "mode": "dry_run|apply", "limit": 6 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/guidance?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-drift?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-actions?window_ms=86400000&limit=6`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 6 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-guidance?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-drift?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-guidance?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance?window_ms=86400000&limit=6`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 6 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-drift?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-trend?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-offenders?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-timeline?window_ms=86400000&limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-matrix?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-summary?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-summary?window_ms=86400000&limit=8`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-guidance?window_ms=86400000&limit=8`
- `POST /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-guidance-run` with `{ "mode": "dry_run|apply", "windowMs": 86400000, "limit": 8 }`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-guidance-run-history?limit=20`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-guidance-run-metrics?window_ms=86400000`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-guidance-state-guidance-state-run-state-guidance-run-anomalies?window_ms=86400000&limit=10`
- `GET /api/session/{sessionId}/graph/score-autopilot/policy-alignment-policy-trend-guidance-policy-guidance-state-history?limit=20`
- `GET /api/session/{sessionId}/graph/query?kind=task&relation=depends_on&q=onboard&done=false&limit=20`
- `GET /api/session/{sessionId}/graph/neighborhood?kind=task&selector=1&depth=2&relation=depends_on&limit=40`
- `GET /api/session/{sessionId}/graph/path?source_kind=task&source=1&target_kind=task&target=2&relation=depends_on&directed=true`
- `GET /api/session/{sessionId}/graph/dependencies?task=<selector>&mode=summary|chain|blockers|impact`
- `GET /api/session/{sessionId}/jobs?limit=100`
- `GET /api/session/{sessionId}/dead-letters?limit=100`
- `GET /api/session/{sessionId}/runtime/health`
- `GET /api/session/{sessionId}/runtime/self-check`
- `GET /api/session/{sessionId}/runtime/profile?limit=200`
- `GET /api/session/{sessionId}/handoff/stats`
- `GET /api/session/{sessionId}/continuity`
- `GET /api/session/{sessionId}/continuity/health`
- `GET /api/session/{sessionId}/continuity/history?limit=50`
- `GET /api/session/{sessionId}/continuity/anomalies?limit=50`
- `GET /api/session/{sessionId}/continuity/incidents?limit=50`
- `GET /api/session/{sessionId}/continuity/next?limit=5`
- `GET /api/session/{sessionId}/continuity/autopilot`
- `GET /api/session/{sessionId}/continuity/autopilot/preview`
- `GET /api/session/{sessionId}/continuity/autopilot/metrics?window_ms=3600000`
- `GET /api/session/{sessionId}/continuity/autopilot/dry-run?force=true`
- `GET /api/session/{sessionId}/continuity/autopilot/guardrails`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-recommendation`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-drift`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-alignment`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-policy?target=aggressive`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-policy/history?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/mode-policy/matrix`
- `GET /api/session/{sessionId}/continuity/autopilot/posture`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/history?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/anomalies?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions?limit=5`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/history?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/metrics?window_ms=3600000`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/anomalies?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/dry-run?index=1`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy-matrix?limit=10`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/history?limit=30`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/metrics?window_ms=3600000`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies?limit=30`
- `POST /api/session/{sessionId}/continuity/autopilot/posture/actions/apply?index=1`
- `POST /api/session/{sessionId}/continuity/autopilot/posture/actions/apply-batch?limit=3`
- `POST /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply?index=1&window_ms=3600000&buckets=6&limit=5`
- `POST /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply-batch?limit=3&window_ms=3600000&buckets=6`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/history?limit=20`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/metrics?window_ms=3600000`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/state?window_ms=3600000&limit=20`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/trend?window_ms=3600000&buckets=6`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/offenders?window_ms=3600000&limit=8`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/summary?window_ms=3600000&buckets=6&limit=8`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/timeline?window_ms=3600000&limit=20`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/matrix?window_ms=3600000&limit=6`
- `GET /api/session/{sessionId}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/guidance?window_ms=3600000&buckets=6&limit=5`
- `POST /api/session/{sessionId}/continuity/autopilot/mode/apply-recommended`
- `GET /api/session/{sessionId}/continuity/autopilot/history?limit=50`
- `POST /api/session/{sessionId}/continuity/autopilot`
- `POST /api/session/{sessionId}/continuity/autopilot/config`
- `POST /api/session/{sessionId}/continuity/autopilot/reset`
- `POST /api/session/{sessionId}/continuity/autopilot/tick?force=true`
- `POST /api/session/{sessionId}/continuity/next/apply`
- `GET /api/session/{sessionId}/continuity/alerts?limit=20`
- `POST /api/session/{sessionId}/continuity/alerts/clear`
- `POST /api/session/{sessionId}/continuity/alerts/drill`
- `POST /api/session/{sessionId}/intent/preview`
- `GET /api/session/{sessionId}/diagnostics`
- `GET /api/session/{sessionId}/snapshot/stats`
- `GET /api/session/{sessionId}/journal/verify`
- `GET /api/session/{sessionId}/checkpoints?limit=20`
- `POST /api/session/{sessionId}/jobs/tick?force=true`
- `POST /api/session/{sessionId}/checkpoints`
- `POST /api/session/{sessionId}/restore`
- `POST /api/session/{sessionId}/restore/checkpoint`
- `POST /api/session/{sessionId}/handoff/start`
- `POST /api/session/{sessionId}/handoff/claim`
- `POST /api/turn`
- `GET /api/stream?sessionId=<id>` (SSE)
- `GET /ws?sessionId=<id>` (WebSocket)

It includes:
- Layered intent envelope compiler
- Deterministic planner/executor
- Model-by-intent router (`deterministic`, `ollama-small`, `ollama-large`)
- route trace metadata (`intentClass`, `confidence`) for explainable routing
- In-memory session store with revisioned broadcast
- Capability registry + policy middleware + action journal
- graph schema contracts with post-mutation invariant validation
- dependency relation guard keeps `depends_on` links acyclic
- Revision conflict protection for cross-device writes (`409 revision_conflict`)
- per-turn performance telemetry (`parseMs`, `executeMs`, `planMs`, `totalMs`, budget status)
- session SLO guard with throttle signal (`breachStreak`, `throttled`, alert tail)

## 🔐 Capability and Policy
Write operations execute through kernel middleware:
1. Capability resolve (`op -> domain + risk`)
2. Policy check (`ok`, `confirmation_required`, or deny)
3. Execution (allowed only)
4. Journal append with policy and diff metadata
5. `kernelTrace` returned to UI for feed rendering

Cross-device write safety:
- each `/api/turn` call carries `baseRevision`
- backend rejects stale write intents with `409` + `revision_conflict`
- client refreshes to latest state before retry
- optional `onConflict: rebase_if_commutative` allows deterministic merge for commutative writes (`add_task`, `add_note`, `add_expense`)
- optional `idempotencyKey` on `/api/turn` deduplicates retried submissions and replays the original turn response without re-running mutations
- browser runtime sends periodic presence heartbeats (`/api/session/{id}/presence`) for active-device continuity

High-risk confirmation:
- `reset memory` is blocked without confirmation
- confirm with: `confirm reset memory`
- incomplete command-like intents are gated for clarification (for example `delete task`)

Scheduler command example:
- `watch task 1 every 10m`
- `remind note Ship weekly summary every 30m`
- `audit open tasks every 15m`
- `summarize expenses daily`
- `list jobs`
- `show dead letters`
- `show runtime health`
- `show presence`
- `prune presence older than 1s`
- `prune presence all`
- `show continuity`
- `show continuity health`
- `show continuity trend`
- `show continuity anomalies`
- `show continuity incidents`
- `show continuity next`
- `show continuity autopilot`
- `show continuity autopilot mode recommendation`
- `show continuity autopilot mode drift`
- `show continuity autopilot mode alignment`
- `show continuity autopilot mode policy aggressive`
- `show continuity autopilot mode policy history`
- `show continuity autopilot mode policy matrix`
- `apply continuity autopilot posture actions`
- `apply continuity autopilot posture action`
- `show continuity autopilot posture actions metrics`
- `show continuity autopilot posture actions anomalies`
- `dry run continuity autopilot posture action`
- `show continuity autopilot posture actions policy matrix`
- `show continuity autopilot posture actions policy history`
- `show continuity autopilot posture actions policy metrics`
- `show continuity autopilot posture actions policy anomalies`
- `show continuity autopilot posture actions policy anomalies history`
- `show continuity autopilot posture actions policy anomalies trend`
- `show continuity autopilot posture actions policy anomalies offenders`
- `show continuity autopilot posture actions policy anomalies state`
- `show continuity autopilot posture actions policy anomalies budget`
- `show continuity autopilot posture actions policy anomalies budget breaches`
- `show continuity autopilot posture actions policy anomalies budget forecast`
- `show continuity autopilot posture actions policy anomalies budget forecast matrix`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions`
- `dry run continuity autopilot posture actions policy anomalies budget forecast guidance action`
- `apply continuity autopilot posture actions policy anomalies budget forecast guidance action`
- `apply continuity autopilot posture actions policy anomalies budget forecast guidance actions`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions history`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions metrics`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies trend`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies state`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies offenders`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies timeline`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies summary`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies matrix`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`
- `dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`
- `apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation history`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation metrics`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation state`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation trend`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation offenders`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation summary`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation timeline`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation matrix`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions`
- `dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action`
- `apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action`
- `apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions history`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions metrics`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions state`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions trend`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions offenders`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions summary`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions timeline`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions matrix`
- `show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions guidance`
- `show continuity autopilot posture actions policy anomalies metrics`
- `show continuity autopilot posture actions history`
- `show continuity autopilot posture actions`
- `show continuity autopilot posture anomalies`
- `show continuity autopilot posture history`
- `show continuity autopilot posture`
- `apply continuity autopilot mode recommendation`
- `show continuity autopilot guardrails`
- `preview continuity autopilot`
- `show continuity autopilot metrics`
- `dry run continuity autopilot`
- `show continuity autopilot history`
- `enable continuity autopilot`
- `disable continuity autopilot`
- `set continuity autopilot cooldown 30s`
- `set continuity autopilot max applies 30 per hour`
- `set continuity autopilot mode safe`
- `set continuity autopilot auto align on`
- `reset continuity autopilot stats`
- `tick continuity autopilot`
- `apply continuity next`
- `show continuity alerts`
- `clear continuity alerts`
- `drill continuity breach`
- `show handoff stats`
- `show runtime profile`
- `show diagnostics`
- `show snapshot stats`
- `verify journal integrity`
- `repair journal integrity`
- `drill policy deny`
- `drill policy confirm`
- `run self check`
- `preview intent add task Draft onboarding checklist`
- `explain intent add task Draft onboarding checklist`
- `retry dead letter 1`
- `purge dead letters`
- `pause job 1` / `resume job 1` / `cancel job 1`
- `undo last`
- `show audit`
- `show audit op add_task`
- `show audit policy ok`
- `show trace`
- `export trace`
- `show trace summary`
- `show trace class mutate`
- `show trace denied`
- `restore preview`
- `restore apply` (requires `confirm restore apply`)
- `checkpoint now`
- `list checkpoints`
- `restore checkpoint latest` (requires `confirm restore checkpoint latest`)
- `show faults`
- `show dependency chain for task 1`
- `show blockers for task 1`
- `show impact for task 1`
- `show graph schema`
- `show graph health`
- `show graph components relation depends_on limit 10`
- `show graph hubs relation depends_on limit 10`
- `show graph events limit 20`
- `show graph summary relation depends_on limit 10`
- `show graph relation matrix relation depends_on limit 100`
- `show graph anomalies limit 20`
- `show graph guidance limit 8`
- `show graph score`
- `show graph score trend window 1h buckets 8`
- `show graph score guidance limit 6`
- `show graph score alerts limit 10`
- `show graph score alerts history window 1h buckets 8 limit 5`
- `show graph score remediation limit 6`
- `show graph score forecast horizon 1h steps 6`
- `show graph score forecast guidance limit 6`
- `show graph score guardrails warn below 75 fail below 60`
- `show graph score autopilot preview limit 6`
- `run graph score autopilot dry run limit 6`
- `run graph score autopilot apply limit 6`
- `show graph score autopilot history limit 20`
- `show graph score autopilot metrics window 24h`
- `show graph score autopilot anomalies window 24h limit 10`
- `show graph score autopilot guidance window 24h limit 8`
- `show graph score autopilot policy window 24h`
- `show graph score autopilot policy drift window 24h`
- `show graph score autopilot policy alignment actions window 24h limit 6`
- `run graph score autopilot policy alignment dry run window 24h limit 6`
- `run graph score autopilot policy alignment apply window 24h limit 6`
- `show graph score autopilot policy alignment history limit 20`
- `show graph score autopilot policy alignment metrics window 24h`
- `show graph score autopilot policy alignment anomalies window 24h limit 10`
- `show graph score autopilot policy alignment guidance window 24h limit 8`
- `show graph score autopilot policy alignment policy window 24h`
- `show graph score autopilot policy alignment policy drift window 24h`
- `show graph score autopilot policy alignment policy guidance window 24h limit 8`
- `run graph score autopilot policy alignment policy dry run window 24h limit 8`
- `run graph score autopilot policy alignment policy apply window 24h limit 8`
- `show graph score autopilot policy alignment policy history limit 20`
- `show graph score autopilot policy alignment policy metrics window 24h`
- `show graph score autopilot policy alignment policy anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend window 24h`
- `show graph score autopilot policy alignment policy trend guidance window 24h limit 6`
- `run graph score autopilot policy alignment policy trend guidance dry run window 24h limit 6`
- `run graph score autopilot policy alignment policy trend guidance apply window 24h limit 6`
- `show graph score autopilot policy alignment policy trend guidance history limit 20`
- `show graph score autopilot policy alignment policy trend guidance metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy drift window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance dry run window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance apply window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance dry run window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance apply window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state trend window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state offenders window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state timeline window 24h limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state matrix window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance dry run window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance apply window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state summary window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state dry run window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state summary window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state guidance window 24h limit 8`
- `run graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state guidance dry run window 24h limit 8`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state guidance run history limit 20`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state guidance run metrics window 24h`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state guidance state guidance state run state guidance run anomalies window 24h limit 10`
- `show graph score autopilot policy alignment policy trend guidance policy guidance state history limit 20`
- `show graph kind task limit 5`
- `show graph neighborhood for task 1 depth 2`
- `show graph path task 1 to task 2 relation depends_on directed on`
- `show open tasks graph limit 20`
- `simulate persist failure on` / `simulate persist failure off`
- `retry persist now`
- `compact journal keep 200` (requires `confirm compact journal keep 200`)
- `start handoff`
- `claim handoff <token>`
- `list files .`
- `read file README.md`
- `grant connector scope web.page.read`
- `show web status`
- `search web local-first os`
- `fetch url https://example.com`
- `open website https://example.com`
- `summarize website https://example.com`
- `show contacts status`
- `grant connector scope contacts.read`
- `show contacts`
- `find contact mike`
- `confirm call mike`
- `remind me to stretch in 10m`
- `show reminders`
- `show reminder status`
- `pause reminder 1`
- `resume reminder 1`
- `cancel reminder 1`

Policy notes for new capability domains:
- `files` domain is workspace-scoped only (paths outside repo root are denied)
- `web` domain requires `web.page.read` scope and allows only public `http/https` URLs (localhost/private targets are denied)
- `contacts` domain requires `contacts.read` scope and returns local scaffold contact matches
- named telephony targets (for example `call mike`) require both `telephony.call.start` and `contacts.read`

Operator guide:
- `docs/OPERATOR_RUNBOOK.md`
- `docs/CONNECTOR_SPEC_V1.md`
- `docs/CONNECTOR_ADAPTER_CONTRACTS.md`

Audit and restore:
- audit log supports policy/domain/risk filters and NDJSON export
- restore endpoint can preview (`apply=false`) or apply (`apply=true`) session rebuild from journal events
- checkpoint endpoints support faster snapshot-based restore with optional tail replay

## 🧠 Frontend (Node only for bundling)
The browser runtime (`app.js`) handles:
- Session bootstrap and URL session propagation
- Intent submission to `/api/turn`
- Realtime merge/apply from WS/SSE
- Local fallback rendering pipeline when backend is unavailable

## ⚙️ Environment
Use `.env` (see `.env.example`):
- `PORT` (default `8787`)
- `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`)
- `OLLAMA_MODEL_SMALL`
- `OLLAMA_MODEL_LARGE`
- `TURN_LATENCY_BUDGET_MS` (default `800`)
- `TURN_HISTORY_MAX_ENTRIES` (default `300`)
- `HANDOFF_LATENCY_BUDGET_MS` (default `500`)
- `PRESENCE_WRITE_MIN_INTERVAL_MS` (default `15000`)
- `CONTINUITY_HISTORY_MAX_ENTRIES` (default `300`)
- `CONTINUITY_ANOMALY_WINDOW` (default `120`)
- `CONTINUITY_ANOMALY_SCORE_DROP` (default `15`)
- `CONTINUITY_AUTOPILOT_COOLDOWN_MS` (default `30000`)
- `CONTINUITY_AUTOPILOT_HISTORY_MAX` (default `200`)
- `CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR` (default `30`)
- `SLO_BREACH_STREAK_FOR_THROTTLE` (default `3`)
- `SLO_THROTTLE_MS` (default `30000`)
- `CHECKPOINT_MAX_COUNT` (default `40`)
- `CHECKPOINT_MAX_AGE_MS` (default `604800000`, 7 days)
- `JOURNAL_MAX_ENTRIES` (default `500`)
- `INTENT_CLARIFICATION_THRESHOLD` (default `0.65`)

Failure-mode hardening:
- persistence write failures now degrade safely (turn execution continues)
- runtime trace + feed expose `faults.persist` diagnostics for operator visibility

## ✅ Venv Location
- Runtime environment is `./.venv` (this is the Python env for backend orchestration).
- Launcher prints the exact `.venv` Python executable it is using.

## 📚 References
- Vercel AI SDK Generative UI patterns: https://vercel.com/academy/ai-sdk/multi-step-and-generative-ui
- React Server Components: https://react.dev/reference/rsc/server-components
- Structured outputs guidance: https://platform.openai.com/docs/guides/structured-outputs
- Ollama runtime: https://github.com/ollama/ollama
