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
- `GET /api/session/{sessionId}`
- `GET /api/session/{sessionId}/presence`
- `POST /api/session/{sessionId}/presence`
- `POST /api/session/{sessionId}/presence/prune`
- `GET /api/session/{sessionId}/journal?limit=50`
- `GET /api/session/{sessionId}/audit?domain=&risk=&ok=&op=&policy_code=&limit=&format=json|ndjson`
- `GET /api/session/{sessionId}/trace?limit=50&ok=&intent_class=&route_reason=&format=json|ndjson`
- `GET /api/session/{sessionId}/trace/summary?limit=200`
- `GET /api/session/{sessionId}/graph?limit=200`
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
- `simulate persist failure on` / `simulate persist failure off`
- `retry persist now`
- `compact journal keep 200` (requires `confirm compact journal keep 200`)
- `start handoff`
- `claim handoff <token>`
- `list files .`
- `read file README.md`
- `fetch url https://example.com`

Policy notes for new capability domains:
- `files` domain is workspace-scoped only (paths outside repo root are denied)
- `web` domain allows only public `http/https` URLs (localhost/private targets are denied)

Operator guide:
- `docs/OPERATOR_RUNBOOK.md`

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
