# GenomeUI Operator Runbook

## Purpose
Operate and verify the capability/policy kernel path quickly, including confirmation-gated actions and audit interpretation.

## Prereqs
- repo bootstrapped (`.venv` + `npm install`)
- backend and frontend running

## Fast Start
1. Start stack:
   - `npm run dev:os`
2. Open:
   - `http://localhost:5173/?session=mysharedsurface`

## Capability + Policy Model
All write operations flow through backend kernel middleware:
- capability resolve (`op -> domain + risk`)
- policy check (`allowed/denied/confirmation_required`)
- operation execution (only if allowed)
- action journal append (`op/domain/risk/policy/diff`)
- `kernelTrace` emission to client

High-risk action rule:
- `reset memory` is blocked unless explicitly confirmed.
- confirmation intent: `confirm reset memory`

## 5-Minute Verification
Use the same session id for all steps.

1. Create task
- intent: `add task verify kernel path`
- expected:
  - Policy: `ok`
  - Diff: tasks `+1`
  - Journal entry with `op=add_task`

2. Trigger blocked high-risk action
- intent: `reset memory`
- expected:
  - Policy: `confirmation_required`
  - No state mutation
  - Feed shows `Required Confirmation`

3. Confirm high-risk action
- intent: `confirm reset memory`
- expected:
  - Policy: `ok`
  - Memory reset applied
  - Journal entry with `op=reset_memory`

## API Checks
Health:
- `GET /api/health`

Session state:
- `GET /api/session/{sessionId}`

Session journal:
- `GET /api/session/{sessionId}/journal?limit=50`

Turn execution:
- `POST /api/turn`

## Audit Interpretation
Each journal item includes:
- `op`: requested operation
- `domain`: tasks/expenses/notes/system
- `risk`: low/medium/high
- `ok`: execution result
- `policy.code`: `ok` | `confirmation_required` | `unknown_capability`
- `diff`: object-count delta for tasks/expenses/notes
- `timestamp`, `sessionId`

How to read quickly:
- `ok=false` + `policy.code=confirmation_required`: expected safety block.
- non-zero `diff` on denied action: bug.
- unknown capability in production intents: parser/planner mismatch.

## Regression Commands
- backend replay tests:
  - `npm run os:test:replay`
- frontend build:
  - `npm run build`

## Known Notes
- `npm run os:test` may fail on local port/process conflicts in some environments; use replay test for deterministic kernel-path validation.
