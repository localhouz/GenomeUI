# GenomeUI Sprint Task Board (2 Weeks)

Scope source: `BUILDPLAN.md` Immediate 2-Week Sprint.

## Sprint Goal
Ship a usable OS core loop where all state mutations flow through capability + policy, with traceable diffs and operator-visible outcomes.

## Ticket List

## T1 - Capability Registry + Policy Middleware
Status: done
Track: A (Runtime/kernel/policy)
Priority: P0
Dependencies: none

Deliverables:
- centralized capability map by operation
- policy evaluation before operation execution
- high-risk confirmation gate

Acceptance:
- unknown capability is denied
- high-risk `reset_memory` denied unless confirmed
- execution result includes policy + capability metadata

Notes:
- implemented in `backend/main.py`
- confirmation command: `confirm reset memory`

---

## T2 - Action Journal + Diff Recorder
Status: done
Track: A
Priority: P0
Dependencies: T1

Deliverables:
- append-only per-session action journal
- per-operation object-count diff
- bounded retention

Acceptance:
- every attempted mutation records a journal event (allowed or denied)
- each journal record includes op/domain/risk/policy/diff/timestamp/sessionId

---

## T3 - Route/Policy Trace in Turn Payload
Status: done
Track: B (Planner/router/evals)
Priority: P0
Dependencies: T1, T2

Deliverables:
- enrich turn trace with route reason and policy summary
- include journal tail in `lastTurn` for UI feed consumption

Acceptance:
- client can render: route target, route reason, policy outcome, diff

Notes:
- backend now emits `kernelTrace` on `POST /api/turn` and `lastTurn.kernelTrace` on session sync payloads

---

## T4 - Frontend Activity Feed: Execution Trace Mode
Status: done
Track: C (Surface UX)
Priority: P0
Dependencies: T3

Deliverables:
- feed sections for `route`, `policy`, `result`, `diff`
- remove stale generic metrics from feed defaults

Acceptance:
- after each intent user sees what was attempted, what ran, what changed

Notes:
- feed now prioritizes `Route`, `Policy`, `Diff`, and `Journal` blocks from `kernelTrace`
- trace is sourced from remote payload and local fallback derivation

---

## T5 - Risk Confirmation UX Flow
Status: done
Track: C
Priority: P0
Dependencies: T1, T4

Deliverables:
- clear UI prompt for blocked high-risk ops
- one-tap follow-up intent generator (e.g. `confirm reset memory`)

Acceptance:
- denied high-risk action is immediately recoverable with explicit confirmation

Notes:
- feed injects `Required Confirmation` with one-tap command when policy returns confirmation hint
- command click submits follow-up intent directly

---

## T6 - Kernel Path Coverage Guard
Status: done
Track: A
Priority: P1
Dependencies: T1

Deliverables:
- guard to prevent planner layer from mutating state directly
- test assertion for kernel-only writes

Acceptance:
- all write operations pass through capability/policy middleware

Notes:
- backend now fingerprints memory after kernel execution and asserts planner/runtime stages do not mutate state
- violation raises runtime error: `planner/runtime stage mutated memory outside kernel path`

---

## T7 - Replay Smoke Tests for Mutations
Status: done
Track: B/E
Priority: P1
Dependencies: T1, T2

Deliverables:
- script-driven replay for canonical write intents
- assertions on policy decisions and diffs

Acceptance:
- `os:test` (or equivalent) verifies mutation path regression-free

Notes:
- added `scripts/replay_smoke.py` with canonical mutation/policy assertions
- command: `npm run os:test:replay`

---

## T8 - Docs + Operator Runbook
Status: done
Track: E (Packaging/devex/ops)
Priority: P1
Dependencies: T1-T5

Deliverables:
- README section: capability/policy model
- runbook for confirmation and audit interpretation

Acceptance:
- new contributor can explain and verify a full write turn in under 10 minutes

Notes:
- added `docs/OPERATOR_RUNBOOK.md` with 5-minute kernel/policy verification path
- added journal API endpoint: `GET /api/session/{sessionId}/journal?limit=50`
- README now documents capability/policy lifecycle and confirmation flow

---

## T9 - Session Handoff Continuity
Status: done
Track: D (Device continuity/networking)
Priority: P1
Dependencies: T3

Deliverables:
- handoff start/claim API wired into realtime session payloads
- frontend handoff controls in activity feed (`start handoff`, `claim handoff <token>`)
- replay + UI coverage for handoff lifecycle

Acceptance:
- claiming handoff updates active device without a full page reload
- handoff state survives restart via session persistence
- automated tests validate start and claim flows

Notes:
- backend emits handoff state in `/api/session/init`, `/api/session/{id}`, and session sync events
- replay test: `scripts/replay_smoke.py`
- UI test: `tests/ui/handoff.spec.js`

---

## T10 - Files/Web Capability Domains
Status: done
Track: A (Runtime/kernel/policy)
Priority: P1
Dependencies: T1

Deliverables:
- new capability ops: `list_files`, `read_file`, `fetch_url`
- policy guardrails for workspace path scope and URL safety
- generated plan support for files/web surfaces

Acceptance:
- file ops denied outside workspace root
- localhost/private URL fetch denied by policy
- replay tests cover both happy path and policy deny path

Notes:
- implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T11 - Undo/Rollback Kernel Primitive
Status: done
Track: A (Runtime/kernel/policy)
Priority: P1
Dependencies: T1

Deliverables:
- `undo last` command routed through capability/policy middleware
- bounded undo snapshot stack persisted per session
- replay and UI assertions for rollback behavior

Acceptance:
- successful mutation can be reverted with `undo last`
- read-only operations do not pollute undo stack
- rollback behavior remains deterministic after restart

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/undo.spec.js`

---

## T12 - Deterministic Conflict Merge
Status: done
Track: D (Device continuity/networking)
Priority: P1
Dependencies: T9

Deliverables:
- stale-write policy keeps strict `409` conflict by default
- opt-in commutative rebase (`onConflict: rebase_if_commutative`) for safe intents
- merge metadata returned in turn payload

Acceptance:
- non-commutative stale writes still reject
- commutative stale writes can be merged deterministically
- replay validates both reject and merge paths

Notes:
- implementation in `backend/main.py` + `app.js`
- replay assertions in `scripts/replay_smoke.py`

---

## T13 - Audit Export + Journal Restore
Status: done
Track: E (Packaging/devex/ops)
Priority: P1
Dependencies: T2

Deliverables:
- filtered capability/policy audit endpoint with NDJSON export
- journal replay/restore endpoint with preview/apply modes
- replay coverage for audit filter/export + restore apply path

Acceptance:
- operator can export audit log scoped by domain/risk/outcome
- restore preview returns deterministic reconstructed state
- restore apply broadcasts synced state and increments revision

Notes:
- implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T14 - Intent-Plane Operator Controls
Status: done
Track: C/E (Surface UX + ops)
Priority: P1
Dependencies: T13

Deliverables:
- intent commands for operator flows (`show audit`, `restore preview`, `restore apply`)
- high-risk confirmation for restore apply (`confirm restore apply`)
- UI test coverage for command-path behavior

Acceptance:
- operator workflows run through the same `/api/turn` intent pipeline
- confirmation affordance appears in feed on blocked restore apply
- end-to-end UI test validates operator flow from input bar

Notes:
- backend command support in `backend/main.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T15 - Snapshot Checkpoints + Tail Replay
Status: done
Track: D/E (Continuity + ops)
Priority: P1
Dependencies: T13

Deliverables:
- persisted session checkpoint snapshots (`graph/jobs/undo/journalSize`)
- checkpoint APIs for create/list/restore
- intent commands for checkpoint create/list/restore latest with confirmation gate
- optional tail replay from checkpoint restore path

Acceptance:
- checkpoint can be created and listed from API and intent plane
- checkpoint restore succeeds and increments session revision
- restore latest via intent requires explicit confirmation

Notes:
- implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T16 - Turn Latency Budget Telemetry
Status: done
Track: B/E (Planner/evals + ops)
Priority: P1
Dependencies: T3

Deliverables:
- per-turn phase timings in kernel trace (`parse`, `execute`, `plan`, `total`)
- configurable performance budget (`TURN_LATENCY_BUDGET_MS`)
- feed visibility for runtime performance block

Acceptance:
- every turn includes runtime performance metrics in `kernelTrace`
- replay validates telemetry presence
- UI shows total vs budget status after intent execution

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T17 - Session SLO Alerts + Throttle Signals
Status: done
Track: B/E (Planner/evals + ops)
Priority: P1
Dependencies: T16

Deliverables:
- session-level SLO streak and alert tracking
- throttle-window signaling in runtime trace
- deterministic route forcing during throttle windows (`reason: slo_throttle`)

Acceptance:
- kernel trace includes `runtime.slo` payload every turn
- replay validates SLO telemetry presence
- UI feed displays SLO block

Notes:
- implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T18 - SLO Stress Replay Harness
Status: done
Track: B/E (Planner/evals + ops)
Priority: P1
Dependencies: T17

Deliverables:
- dedicated stress replay script for SLO throttle behavior
- automated assertions for breach streak and throttle route forcing
- integrated into verification command chain

Acceptance:
- stress replay reliably reproduces throttle activation
- verify pipeline fails if throttle semantics regress

Notes:
- script: `scripts/slo_stress.py`
- npm script: `os:test:slo`

---

## T19 - Checkpoint Retention + Restore Diagnostics
Status: done
Track: D/E (Continuity + ops)
Priority: P1
Dependencies: T15

Deliverables:
- checkpoint retention policy by max count and max age
- restore diagnostics in runtime trace/session state
- restore diagnostics surfaced in operator feed block

Acceptance:
- checkpoints prune according to retention settings
- restore actions record structured diagnostics (`source`, `checkpointId`, replay metadata)
- replay and UI tests assert restore diagnostics visibility

Notes:
- implementation in `backend/main.py` and `app.js`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T20 - Persistence Failure Degradation + Fault Signals
Status: done
Track: E (ops/reliability)
Priority: P1
Dependencies: T13

Deliverables:
- safe persist wrapper prevents turn crashes on disk write failures
- operator commands for fault simulation and inspection
- runtime fault diagnostics exposed in trace/feed/session payload

Acceptance:
- persistence failures do not abort successful intent execution
- `faults.persist` reflects degraded state and recovery
- replay/UI tests validate fault simulation and visibility

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T21 - Journal Retention + Safe Compaction
Status: done
Track: E (ops/reliability)
Priority: P1
Dependencies: T2

Deliverables:
- configurable journal retention cap (`JOURNAL_MAX_ENTRIES`)
- confirmation-gated journal compaction intent/API
- checkpoint journal-base normalization after compaction

Acceptance:
- journal compaction reduces retained entries near requested keep size
- compaction requires explicit confirmation in intent plane
- replay/UI tests validate compact flow and confirmation affordance

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T22 - Intent Clarification Gate
Status: done
Track: B/C (planner safety + UX)
Priority: P1
Dependencies: T1, T3

Deliverables:
- detect incomplete command-like intents before execution
- return deterministic clarification response with examples
- expose clarification policy signal in journal/feed via intent plane

Acceptance:
- ambiguous command intents do not execute side effects
- turn payload includes clarification marker + policy code
- replay and UI tests validate clarification behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T23 - Graph Contract Enforcement
Status: done
Track: A/B (kernel state integrity)
Priority: P1
Dependencies: T2, T6

Deliverables:
- entity/relation/event schema invariants for graph state
- post-mutation validation with rollback on violation
- relation constraint checks (`depends_on` task->task only, no self-link)

Acceptance:
- invalid graph mutations are blocked without side effects
- persisted invalid graph state is repaired to safe projection on session ensure
- replay tests cover invalid relation constraint path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T24 - Dependency Chain Query + Cycle-Safe Introspection
Status: done
Track: B/C (world-model query + UX trace)
Priority: P1
Dependencies: T23

Deliverables:
- transitive dependency chain query intent (`show dependency chain for task <id>`)
- cycle prevention for `depends_on` links
- graph context depth signal for dependency topology

Acceptance:
- chain query reports deterministic depth for acyclic task graph
- cyclic `depends_on` mutation is rejected with explicit reason
- replay/UI tests validate chain query and graph-context relation signals

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/trace.spec.js`

---

## T25 - Dependency Health Queries (Blockers + Impact)
Status: done
Track: B/C (world-model query + UX trace)
Priority: P1
Dependencies: T24

Deliverables:
- blocker query (`show blockers for task <id>`)
- transitive impact query (`show impact for task <id>`)
- graph context signals for blocked/root task topology

Acceptance:
- blocker query reports direct dependency targets
- impact query reports transitive dependent count
- replay coverage validates blocker/impact semantics

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T26 - Dependency Analysis API
Status: done
Track: D/E (ops + testability)
Priority: P1
Dependencies: T25

Deliverables:
- structured dependency analysis endpoint for task selectors
- mode filters (`summary`, `chain`, `blockers`, `impact`)
- replay assertions for endpoint depth and impact counts

Acceptance:
- endpoint resolves task by selector and returns deterministic dependency payload
- invalid mode is rejected; missing task returns not found
- replay validates chain depth and impact counts on known graph fixture

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T27 - Intent-Class Route Metadata
Status: done
Track: B/E (planner routing + observability)
Priority: P1
Dependencies: T22

Deliverables:
- intent classification (`mutate`, `ops`, `graph_query`, `question`, query variants)
- route payload enrichments (`intentClass`, `confidence`)
- replay assertions for route metadata and clarification route reason

Acceptance:
- every turn route trace includes intent class and confidence
- clarification-gated intents report `route.reason = clarification_gate`
- UI system feed can display intent class line from route trace

Notes:
- backend implementation in `backend/main.py`
- frontend route display in `app.js`
- replay coverage in `scripts/replay_smoke.py`

---

## T28 - Turn Trace History + Intent Command
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T27

Deliverables:
- persisted per-session turn history with bounded retention
- trace endpoint (`/api/session/{id}/trace`)
- intent-plane command (`show trace`) for operator workflows

Acceptance:
- each turn appends compact route/execution/perf trace record
- trace endpoint returns structured entries for diagnostics
- replay and UI operator tests validate trace command and endpoint

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T29 - Trace Filters (Class/Reason/Outcome)
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T28

Deliverables:
- trace endpoint filters (`ok`, `intent_class`, `route_reason`)
- intent command variants (`show trace class <x>`, `show trace reason <x>`, `show trace denied`)
- replay/UI coverage for filter command path

Acceptance:
- filtered trace API returns deterministic subset
- trace command filters execute through `/api/turn` policy path
- replay validates filtered endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T30 - Trace Summary (API + Intent Plane)
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T29

Deliverables:
- trace summary endpoint (`/trace/summary`) with counts/latency aggregates
- intent command (`show trace summary`)
- replay/UI coverage for summary command and endpoint

Acceptance:
- summary includes total, ok/denied, average latency, and class/reason breakdown
- summary command runs through kernel policy path
- replay validates endpoint structure and summary command lines

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T31 - Scheduler Dead-Letter Queue
Status: done
Track: D/E (runtime reliability + ops)
Priority: P1
Dependencies: T4, T30

Deliverables:
- dead-letter queue for repeated scheduled-job failures
- dead-letter API (`/api/session/{id}/dead-letters`)
- intent commands (`show dead letters`, `retry dead letter <id>`, `purge dead letters`)

Acceptance:
- job failures after retry budget are moved to dead-letter queue
- operator can inspect, retry, and purge dead letters via intent plane
- replay validates failing probe lifecycle and dead-letter transitions

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T32 - Runtime Health Surface
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T31

Deliverables:
- runtime health endpoint aggregating jobs/slo/faults/dead-letters
- intent command (`show runtime health`)
- replay/UI coverage for health command path

Acceptance:
- health endpoint returns scheduler/dead-letter/fault/slo/perf sections
- command renders compact health diagnostics in trace feed
- replay validates both endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T33 - Runtime Self-Check
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T32

Deliverables:
- self-check endpoint (`/runtime/self-check`) with contract/fault/scheduler checks
- intent command (`run self check`)
- replay/UI coverage for self-check surfaces

Acceptance:
- endpoint returns overall status and named checks list
- intent command returns compact check lines in feed
- replay validates endpoint shape and command output signal

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T34 - Intent Explainability Command
Status: done
Track: B/E (planner introspection + ops)
Priority: P1
Dependencies: T27

Deliverables:
- intent-plane command `explain intent <text>` for pre-execution inspection
- summary output includes class/confidence/predicted route/domain/risk hints
- replay/UI coverage for explainability path

Acceptance:
- command runs as non-mutating low-risk op
- output includes `class:` and `route:` lines
- replay and UI tests validate command visibility and output structure

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T35 - Intent Preview Dry-Run
Status: done
Track: B/E (planner safety + ops)
Priority: P1
Dependencies: T34

Deliverables:
- intent command `preview intent <text>` for zero-side-effect policy/capability simulation
- API endpoint `/api/session/{id}/intent/preview`
- replay/UI coverage for preview output and endpoint

Acceptance:
- preview command does not mutate state
- output includes class/confidence/route and op policy evaluations
- preview endpoint returns structured write evaluations for intent

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T36 - Unified Diagnostics Bundle
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T33, T35

Deliverables:
- diagnostics endpoint (`/api/session/{id}/diagnostics`) aggregating health/self-check/trace summary
- intent command (`show diagnostics`) for quick operator snapshot
- replay/UI coverage for diagnostics API + command

Acceptance:
- diagnostics payload includes health, selfCheck, traceSummary, and journal head metadata
- diagnostics command surfaces compact status lines including self-check state
- replay validates endpoint shape and command output

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T37 - Snapshot Stats Surface
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T36

Deliverables:
- snapshot stats endpoint (`/snapshot/stats`) for compact state-cardinality signals
- intent command (`show snapshot stats`)
- replay/UI coverage for endpoint and command behavior

Acceptance:
- endpoint returns revision + graph/jobs/journal/trace/checkpoint/undo counts
- command surfaces compact graph/jobs/journal summary lines
- replay validates endpoint payload and command output signals

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T38 - Journal Integrity Verify/Repair
Status: done
Track: E (ops/reliability)
Priority: P1
Dependencies: T37

Deliverables:
- journal integrity verify endpoint (`/journal/verify`)
- intent commands (`verify journal integrity`, `repair journal integrity`)
- replay/UI coverage for integrity command path

Acceptance:
- verify returns `valid/count/issues` payload
- repair canonicalizes/removes malformed entries and reports before/after counts
- replay validates endpoint/report shape and command execution

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T39 - Policy Drill Commands
Status: done
Track: E (ops/policy observability)
Priority: P1
Dependencies: T38

Deliverables:
- intent commands for synthetic policy pathways (`drill policy deny`, `drill policy confirm`)
- deterministic denied outcomes with expected policy codes
- replay/UI coverage for deny/confirm drill visibility

Acceptance:
- deny drill emits `unknown_capability` policy code
- confirm drill emits `confirmation_required` policy code
- drills do not mutate core session state

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T40 - Audit Filters (Op + Policy Code)
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T39

Deliverables:
- audit API filter support for `op` and `policy_code`
- intent command variants (`show audit op <op>`, `show audit policy <code>`)
- replay/UI coverage for filtered audit paths

Acceptance:
- endpoint filter returns only matching op/policy entries
- command filter runs through intent plane and yields filtered summary
- replay validates endpoint and command filtering semantics

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T41 - Trace Export (NDJSON + Intent Command)
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T40

Deliverables:
- trace API export format support (`format=ndjson|jsonl`) on `/api/session/{id}/trace`
- intent command `export trace` (`export trace limit <n>`) for operator path parity
- replay/UI coverage for command and API export response

Acceptance:
- trace endpoint returns `application/x-ndjson` when export format is requested
- `export trace` command is non-mutating and returns preview lines from recent trace entries
- replay/UI tests assert command visibility and NDJSON export availability

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T42 - Runtime Profile (Latency Percentiles)
Status: done
Track: E (ops/observability)
Priority: P1
Dependencies: T41

Deliverables:
- runtime profile endpoint (`/api/session/{id}/runtime/profile?limit=<n>`)
- intent command (`show runtime profile`, optional sample size)
- replay/UI coverage for profile API + command

Acceptance:
- endpoint returns sample count/limit, latency stats (`avg`, `p50`, `p95`, `max`), and outcome rates
- command surfaces compact latency and budget lines in operator feed
- replay/UI tests validate endpoint shape and command visibility

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T43 - Handoff Continuity Stress Harness
Status: done
Track: D/E (continuity + testability)
Priority: P1
Dependencies: T42

Deliverables:
- stress harness script (`scripts/handoff_stress.py`) for repeated handoff start/claim cycles
- npm command (`npm run os:test:handoff`)
- docs update for operator verification flow

Acceptance:
- executes 100 deterministic handoff start/claim cycles without token or state drift
- final session state reflects last claimed active device and no pending handoff
- command runs locally in venv-first flow

Notes:
- harness implementation in `scripts/handoff_stress.py`
- package command in `package.json`
- docs update in `README.md`

---

## T44 - Handoff Telemetry Surface
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T43

Deliverables:
- handoff stats API endpoint (`/api/session/{id}/handoff/stats`)
- intent command (`show handoff stats`) for operator visibility
- telemetry capture for starts/claims/errors and claim latency summary

Acceptance:
- endpoint returns starts/claims/expired/invalid and latency (`avg`, `last`, `p95`, `max`)
- command surfaces compact continuity metrics in feed
- replay/UI tests validate endpoint and command path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T45 - Presence Heartbeat Model
Status: done
Track: D/E (continuity + runtime state)
Priority: P1
Dependencies: T44

Deliverables:
- presence heartbeat APIs (`POST/GET /api/session/{id}/presence`)
- session sync payload includes `presence` for cross-device state continuity
- intent command `show presence` for operator visibility

Acceptance:
- multiple devices can heartbeat into one session with `deviceId/label/platform`
- presence API returns active vs stale device counts using timeout window
- replay/UI tests validate endpoint behavior and intent command output

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T46 - Turn Idempotency Keys
Status: done
Track: D/A (continuity + kernel safety)
Priority: P1
Dependencies: T45

Deliverables:
- `/api/turn` accepts optional `idempotencyKey`
- session-scoped response cache for duplicate intent submissions with same key
- replay coverage proving duplicate retries do not execute duplicate mutations

Acceptance:
- second submit with same `sessionId + intent + idempotencyKey` returns reused response
- duplicate retry does not append duplicate state mutation
- replay suite validates reuse marker and resulting state cardinality

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`

---

## T47 - Client Presence Heartbeat Runtime
Status: done
Track: D/C (continuity + UX runtime)
Priority: P1
Dependencies: T46

Deliverables:
- frontend periodic presence heartbeat to `/api/session/{id}/presence`
- remote sync/state handling includes `presence` in session payload application
- runtime/system feed surfaces presence counts from kernel trace

Acceptance:
- active browser session automatically registers as present without manual commands
- handoff flow test verifies presence endpoint reports active devices
- replay diagnostics include presence summary shape

Notes:
- frontend implementation in `app.js`
- backend runtime trace/health updates in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/handoff.spec.js`

---

## T48 - Presence Prune Controls
Status: done
Track: D/E (continuity + operations)
Priority: P1
Dependencies: T47

Deliverables:
- presence prune API (`POST /api/session/{id}/presence/prune`)
- intent command (`prune presence all`) through policy/capability path
- presence payload adds `staleCount` for cleanup visibility

Acceptance:
- operator can clear presence device entries deterministically
- command and endpoint both report removed/remaining counts
- replay/UI tests cover prune command visibility and endpoint behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T49 - Continuity Report Surface
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T48

Deliverables:
- continuity API endpoint (`/api/session/{id}/continuity`)
- intent command (`show continuity`) for operator plane
- continuity summary integrated into diagnostics payload

Acceptance:
- endpoint reports presence/handoff/idempotency continuity metrics in one payload
- command surfaces compact continuity lines (devices, handoff p95, idempotency cache)
- replay/UI tests validate endpoint shape and command visibility

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T50 - Auto Presence Prune + Aging Command
Status: done
Track: D/E (runtime continuity + ops)
Priority: P1
Dependencies: T49

Deliverables:
- scheduler-driven stale-presence auto-prune (`presence_prune_auto`)
- intent syntax for age-based prune (`prune presence older than <n>ms|s|m`)
- continuity summary includes presence prune telemetry

Acceptance:
- stale presence entries are pruned during runtime loop without manual input
- age-based prune command reports mode and removed/remaining counts
- replay/UI tests validate command and continuity summary signals

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T51 - Handoff Latency Budget Telemetry
Status: done
Track: D/E (continuity + reliability telemetry)
Priority: P1
Dependencies: T50

Deliverables:
- configurable handoff latency budget (`HANDOFF_LATENCY_BUDGET_MS`)
- handoff breach counters and last-breach timestamp in handoff stats payload
- continuity summary includes handoff budget and breach count

Acceptance:
- each handoff claim evaluates latency against configured budget
- handoff stats expose budget + breach counters for operators
- replay tests validate budget/breach fields in continuity and handoff stats

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- config/docs updates in `.env.example` and `README.md`

---

## T52 - Continuity Alert History
Status: done
Track: D/E (continuity + alerting)
Priority: P1
Dependencies: T51

Deliverables:
- handoff budget-breach alert history stored in handoff stats
- continuity alerts endpoint (`/api/session/{id}/continuity/alerts`)
- intent command (`show continuity alerts`)

Acceptance:
- breach events append timestamped alert entries (`claimMs`, `budgetMs`, `deviceId`)
- alerts endpoint returns bounded alert items for operator diagnostics
- replay/UI tests validate command and endpoint behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T53 - Continuity Alert Reset Controls
Status: done
Track: D/E (continuity + operator controls)
Priority: P1
Dependencies: T52

Deliverables:
- continuity alerts clear endpoint (`POST /api/session/{id}/continuity/alerts/clear`)
- intent command (`clear continuity alerts`)
- replay/UI coverage for endpoint and command reset flow

Acceptance:
- clear endpoint reports cleared count and leaves alert list empty
- clear command runs through intent plane and returns cleared counters
- replay/UI tests validate reset behavior without regressions

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T54 - Continuity Breach Drill
Status: done
Track: D/E (continuity + operator testing)
Priority: P1
Dependencies: T53

Deliverables:
- continuity drill endpoint (`POST /api/session/{id}/continuity/alerts/drill`)
- intent command (`drill continuity breach`)
- replay/UI coverage for deterministic breach injection

Acceptance:
- drill path appends a synthetic continuity alert with budget overrun fields
- command and endpoint both expose drill result in operator workflows
- replay/UI tests validate drill visibility and resulting alert availability

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T55 - Continuity Health Scoring
Status: done
Track: D/E (continuity + reliability posture)
Priority: P1
Dependencies: T54

Deliverables:
- continuity health classifier (`healthy|degraded|critical`) with score and reasons
- health endpoint (`/api/session/{id}/continuity/health`)
- intent command (`show continuity health`)

Acceptance:
- continuity payload includes computed `health` object
- health endpoint and command expose status/score/reason lines for operators
- replay/UI tests validate endpoint shape and command visibility

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`

---

## T56 - Presence Heartbeat Coalescing
Status: done
Track: D/E (continuity + runtime efficiency)
Priority: P1
Dependencies: T55

Deliverables:
- coalesced presence heartbeat writes for rapid identical updates
- presence stats counters (`heartbeatWrites`, `heartbeatCoalesced`)
- continuity summary includes heartbeat write/coalesce signals

Acceptance:
- rapid identical heartbeat does not increment session revision
- presence payload reports write/coalesced counters
- replay tests validate coalesced behavior and revision stability

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- config/docs updates in `.env.example` and `README.md`

---

## T57 - Continuity Trend History
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T56

Deliverables:
- per-session continuity history snapshots with bounded retention
- continuity history endpoint (`/api/session/{id}/continuity/history`)
- intent command (`show continuity trend`)

Acceptance:
- continuity snapshots append on core continuity-affecting events and turns
- endpoint returns recent timeline entries with status/score/device/breach signals
- replay/UI tests validate endpoint shape and command output visibility

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- config/docs updates in `.env.example` and `README.md`

---

## T58 - Continuity Anomaly Detection Surface
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T57

Deliverables:
- anomaly detector over continuity trend history for regression/spike events
- continuity anomalies endpoint (`/api/session/{id}/continuity/anomalies`)
- intent command (`show continuity anomalies`)
- diagnostics bundle includes compact anomaly summary

Acceptance:
- endpoint returns bounded anomaly entries and severity summary from recent continuity history
- command surfaces anomaly status lines through intent plane
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- config/docs updates in `.env.example` and `README.md`

---

## T59 - Continuity Incident Feed (Merged Alerts + Anomalies)
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T58

Deliverables:
- unified continuity incident builder combining anomaly events and handoff breach alerts
- continuity incidents endpoint (`/api/session/{id}/continuity/incidents`)
- intent command (`show continuity incidents`)
- diagnostics bundle includes compact continuity incident summary

Acceptance:
- endpoint returns bounded incident list with severity/category/type/detail
- command surfaces incident summary lines in operator plane
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T60 - Continuity Next-Action Planner
Status: done
Track: D/E (continuity + operator workflow)
Priority: P1
Dependencies: T59

Deliverables:
- continuity next-action planner built from health + incidents summaries
- next-action endpoint (`/api/session/{id}/continuity/next`)
- intent command (`show continuity next`)
- diagnostics bundle includes compact next-action posture summary

Acceptance:
- endpoint returns bounded prioritized actions (`priority/title/command/reason`)
- command surfaces top action set in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T61 - Continuity Next-Action Auto-Apply
Status: done
Track: D/E (continuity + operator automation)
Priority: P1
Dependencies: T60

Deliverables:
- safe auto-apply routine for continuity next actions
- API endpoint (`POST /api/session/{id}/continuity/next/apply`)
- intent command (`apply continuity next`)

Acceptance:
- endpoint applies one safe remediation action when available and returns structured report
- command executes via intent plane and surfaces applied/no-op marker
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T62 - Continuity Autopilot Runtime
Status: done
Track: D/E (continuity + runtime automation)
Priority: P1
Dependencies: T61

Deliverables:
- session-scoped continuity autopilot state with cooldown + counters
- autopilot APIs (`GET/POST /continuity/autopilot`, `POST /continuity/autopilot/tick`)
- intent commands (`show/enable/disable/tick continuity autopilot`)
- scheduler integration for periodic autopilot evaluation

Acceptance:
- operators can toggle autopilot and inspect status/counters via endpoint and intent plane
- manual tick endpoint returns deterministic applied/noop reason payload
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- config/docs updates in `.env.example` and `README.md`

---

## T63 - Continuity Autopilot History Audit
Status: done
Track: D/E (continuity + auditability)
Priority: P1
Dependencies: T62

Deliverables:
- persisted continuity autopilot history events with bounded retention
- autopilot history endpoint (`GET /api/session/{id}/continuity/autopilot/history`)
- intent command (`show continuity autopilot history`)

Acceptance:
- autopilot state changes and tick outcomes append structured history events
- endpoint returns bounded history entries for operator audit
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- config/docs updates in `.env.example` and `README.md`

---

## T64 - Continuity Autopilot Cooldown Controls
Status: done
Track: D/E (continuity + runtime controls)
Priority: P1
Dependencies: T63

Deliverables:
- runtime cooldown config endpoint (`POST /api/session/{id}/continuity/autopilot/config`)
- intent command (`set continuity autopilot cooldown <n>ms|s|m`)
- cooldown updates recorded in autopilot history

Acceptance:
- operators can update autopilot cooldown without restart or env changes
- cooldown updates visible in autopilot status/history surfaces
- replay/UI tests validate endpoint and intent command path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T65 - Continuity Autopilot Rate Limit Guard
Status: done
Track: D/E (continuity + safety guardrails)
Priority: P1
Dependencies: T64

Deliverables:
- rolling hourly apply-limit guard for continuity autopilot
- config support for `maxAppliesPerHour` via API and intent plane
- rate-limit events recorded in autopilot history

Acceptance:
- autopilot ticks honor max applies/hour and return `reason: rate_limited` when saturated
- operators can update max applies/hour dynamically
- replay/UI tests validate config and guard behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- config/docs updates in `.env.example` and `README.md`

---

## T66 - Continuity Autopilot Reset Controls
Status: done
Track: D/E (continuity + operator controls)
Priority: P1
Dependencies: T65

Deliverables:
- autopilot reset endpoint (`POST /api/session/{id}/continuity/autopilot/reset`)
- intent command (`reset continuity autopilot stats`)
- reset workflow preserves a reset audit event and supports optional history clear

Acceptance:
- reset clears autopilot counters and rate-window timestamps deterministically
- command executes through intent plane and returns reset markers
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T67 - Continuity Autopilot Preview/Explain Surface
Status: done
Track: D/E (continuity + operator explainability)
Priority: P1
Dependencies: T66

Deliverables:
- autopilot preview endpoint (`GET /api/session/{id}/continuity/autopilot/preview`)
- intent command (`preview continuity autopilot`)
- diagnostics includes autopilot preview reason signal

Acceptance:
- preview reports `canRun`, gating `reason`, cooldown wait, rate-limit usage, and candidate command
- command surfaces preview state through intent plane
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T68 - Continuity Autopilot Metrics Surface
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T67

Deliverables:
- autopilot metrics endpoint (`GET /api/session/{id}/continuity/autopilot/metrics`)
- intent command (`show continuity autopilot metrics`)
- diagnostics adds compact recent-event signal

Acceptance:
- metrics reports recent/change/applied counters plus reason/source breakdown in a window
- command surfaces summary and top reason through intent plane
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T69 - Continuity Autopilot Dry-Run Surface
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T68

Deliverables:
- autopilot dry-run endpoint (`GET /api/session/{id}/continuity/autopilot/dry-run`)
- intent command (`dry run continuity autopilot`)
- projected snapshot delta payload for zero-side-effect simulation

Acceptance:
- dry-run reports projected tick outcome reason without mutating live session state
- command surfaces projected run/changed/reason lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T70 - Continuity Autopilot Guardrails Surface
Status: done
Track: D/E (continuity + safety guardrails)
Priority: P1
Dependencies: T69

Deliverables:
- guardrail evaluator for autopilot execution conditions
- guardrails endpoint (`GET /api/session/{id}/continuity/autopilot/guardrails`)
- intent command (`show continuity autopilot guardrails`)

Acceptance:
- autopilot tick blocks with explicit reason when guardrails fail (e.g. persistence degraded, handoff pending)
- preview and diagnostics surfaces include guardrail-informed reason signal
- replay/UI tests validate endpoint, command, and blocked tick path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T71 - Continuity Autopilot Mode Profiles
Status: done
Track: D/E (continuity + control policy)
Priority: P1
Dependencies: T70

Deliverables:
- autopilot mode profiles (`safe|normal|aggressive`) persisted in autopilot state
- mode config support through API and intent command (`set continuity autopilot mode <mode>`)
- mode signals surfaced in autopilot show/preview/tick outputs

Acceptance:
- operators can switch mode at runtime without restart
- safe mode constrains action selection and effective hourly apply budget
- replay/UI tests validate mode config endpoint and command paths

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T72 - Continuity Autopilot Mode Recommendation
Status: done
Track: D/E (continuity + operator guidance)
Priority: P1
Dependencies: T71

Deliverables:
- mode recommendation endpoint (`GET /api/session/{id}/continuity/autopilot/mode-recommendation`)
- intent command (`show continuity autopilot mode recommendation`)
- recommendation signal surfaced in diagnostics autopilot summary

Acceptance:
- recommendation reports current mode, suggested mode, and reasoned signals
- command surfaces recommendation lines through intent plane
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T73 - Apply Recommended Autopilot Mode
Status: done
Track: D/E (continuity + operator workflow)
Priority: P1
Dependencies: T72

Deliverables:
- mode apply endpoint (`POST /api/session/{id}/continuity/autopilot/mode/apply-recommended`)
- intent command (`apply continuity autopilot mode recommendation`)
- history audit event for recommendation-driven mode change

Acceptance:
- operators can adopt suggested mode in one action from API or intent plane
- response reports previous/recommended mode and change marker
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T74 - Autopilot Mode Drift Detection
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T73

Deliverables:
- mode drift endpoint (`GET /api/session/{id}/continuity/autopilot/mode-drift`)
- intent command (`show continuity autopilot mode drift`)
- diagnostics autopilot summary includes drift flag

Acceptance:
- reports whether current mode deviates from recommended mode with reasons
- command surfaces drift state in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T75 - Autopilot Mode Auto-Align
Status: done
Track: D/E (continuity + adaptive control)
Priority: P1
Dependencies: T74

Deliverables:
- autopilot config flag `autoAlignMode` (API + intent command)
- tick path can align mode to recommendation when enabled
- mode alignment events recorded in autopilot history

Acceptance:
- operators can enable/disable auto-align at runtime
- autopilot show/config surfaces expose auto-align status
- replay/UI tests validate auto-align config command path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T76 - Autopilot Mode Alignment Telemetry
Status: done
Track: D/E (continuity + observability)
Priority: P1
Dependencies: T75

Deliverables:
- mode alignment endpoint (`GET /api/session/{id}/continuity/autopilot/mode-alignment`)
- intent command (`show continuity autopilot mode alignment`)
- autopilot state/diagnostics include alignment counters

Acceptance:
- endpoint returns alignment summary (`aligned`, `lastAlignAt`, `count`) and recent alignment events
- command surfaces alignment counters and recent events in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T77 - Autopilot Mode Transition Policy
Status: done
Track: D/E (continuity + safety policy)
Priority: P1
Dependencies: T76

Deliverables:
- mode policy endpoint (`GET /api/session/{id}/continuity/autopilot/mode-policy`)
- intent command (`show continuity autopilot mode policy <mode>`)
- enforcement of mode policy on mode config/apply-recommended paths

Acceptance:
- aggressive mode transitions are blocked when guardrails are active or continuity health is not healthy
- command and endpoint expose policy code/reason/signals
- replay/UI tests validate endpoint, command, and blocked policy path

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T78 - Mode Policy History Audit
Status: done
Track: D/E (continuity + policy audit)
Priority: P1
Dependencies: T77

Deliverables:
- mode policy history endpoint (`GET /api/session/{id}/continuity/autopilot/mode-policy/history`)
- intent command (`show continuity autopilot mode policy history`)
- explicit policy-block history events for mode config/apply paths

Acceptance:
- endpoint returns summary and recent policy-relevant events (allowed vs blocked)
- command surfaces policy history summary in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T79 - Mode Policy Matrix Surface
Status: done
Track: D/E (continuity + policy observability)
Priority: P1
Dependencies: T78

Deliverables:
- mode policy matrix endpoint (`GET /api/session/{id}/continuity/autopilot/mode-policy/matrix`)
- intent command (`show continuity autopilot mode policy matrix`)
- matrix rows summarize allow/block outcome for `safe|normal|aggressive`

Acceptance:
- endpoint returns deterministic 3-row policy matrix with summary counts
- command surfaces compact matrix lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T80 - Autopilot Posture Surface
Status: done
Track: D/E (continuity + operator visibility)
Priority: P1
Dependencies: T79

Deliverables:
- posture endpoint (`GET /api/session/{id}/continuity/autopilot/posture`)
- intent command (`show continuity autopilot posture`)
- unified top-line posture summary (mode/recommendation/drift/guardrails/policy/alignment)

Acceptance:
- endpoint returns compact autopilot posture object with core decision signals
- command surfaces posture summary lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T81 - Autopilot Posture History
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T80

Deliverables:
- posture history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/history`)
- intent command (`show continuity autopilot posture history`)
- posture snapshots appended across autopilot mutation/tick paths

Acceptance:
- endpoint returns posture snapshot items plus drifted/count summary
- command surfaces compact posture-history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T82 - Autopilot Posture Anomaly Surface
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T81

Deliverables:
- posture anomaly endpoint (`GET /api/session/{id}/continuity/autopilot/posture/anomalies`)
- intent command (`show continuity autopilot posture anomalies`)
- anomaly detection over posture snapshots (mode changes, drift start, guardrail increases, critical reason shifts)

Acceptance:
- endpoint returns anomaly items with compact summary/counts
- command surfaces anomaly lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T83 - Autopilot Posture Actions Surface
Status: done
Track: D/E (continuity + operator workflow)
Priority: P1
Dependencies: T82

Deliverables:
- posture actions endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions`)
- intent command (`show continuity autopilot posture actions`)
- deterministic remediation actions derived from posture + posture anomalies

Acceptance:
- endpoint returns prioritized actionable commands with reasons
- command surfaces action queue in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T84 - Autopilot Posture Action Apply
Status: done
Track: D/E (continuity + operator workflow)
Priority: P1
Dependencies: T83

Deliverables:
- posture action apply endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/apply`)
- intent command (`apply continuity autopilot posture action`)
- deterministic application of top posture remediation action for safe/mutable commands

Acceptance:
- endpoint returns applied/no-op report with selected action metadata
- command surfaces apply result in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T85 - Autopilot Posture Batch Apply
Status: done
Track: D/E (continuity + operator workflow)
Priority: P1
Dependencies: T84

Deliverables:
- batch apply endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/apply-batch`)
- intent command (`apply continuity autopilot posture actions`)
- deterministic bounded loop to apply top posture actions until stop condition

Acceptance:
- endpoint returns attempted/applied counts with per-step results
- command surfaces batch apply summary in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T86 - Posture Action Audit History
Status: done
Track: D/E (continuity + operator auditability)
Priority: P1
Dependencies: T85

Deliverables:
- posture action history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/history`)
- intent command (`show continuity autopilot posture actions history`)
- persisted posture-action audit events for single and batch apply paths

Acceptance:
- endpoint returns action history summary with applied/no-op counts and recent events
- command surfaces posture action audit timeline in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T87 - Posture Action Metrics Surface
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T86

Deliverables:
- posture action metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/metrics`)
- intent command (`show continuity autopilot posture actions metrics`)
- rolling metrics over posture-action audit events (applied/changed rates, top commands/reasons)

Acceptance:
- endpoint returns summary and count maps over a bounded window
- command surfaces compact metrics in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T88 - Posture Action Anomaly Surface
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T87

Deliverables:
- posture action anomaly endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/anomalies`)
- intent command (`show continuity autopilot posture actions anomalies`)
- anomaly detection over posture-action audit timeline (failures, noop streaks, repeated noop commands)

Acceptance:
- endpoint returns anomaly summary/counts and recent anomaly items
- command surfaces compact anomaly lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T89 - Posture Action Dry-Run Surface
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T88

Deliverables:
- posture action dry-run endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/dry-run`)
- intent command (`dry run continuity autopilot posture action`)
- policy-aware dry-run output for selected posture action without applying mutations

Acceptance:
- endpoint returns selected action preview, mapped op (if any), policy evaluation, and appliable flag
- command surfaces dry-run readiness and policy code in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T90 - Posture Action Policy Matrix
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T89

Deliverables:
- posture action policy matrix endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy-matrix`)
- intent command (`show continuity autopilot posture actions policy matrix`)
- matrix rows showing action-by-action allow/block/informational posture from policy evaluation

Acceptance:
- endpoint returns summary (`allowed/blocked/informational`) and matrix items with policy codes
- command surfaces compact matrix lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T91 - Posture Action Policy History Audit
Status: done
Track: D/E (continuity + operator auditability)
Priority: P1
Dependencies: T90

Deliverables:
- posture action policy history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/history`)
- intent command (`show continuity autopilot posture actions policy history`)
- persisted policy-decision audit events from dry-run and apply pathways

Acceptance:
- endpoint returns policy decision history summary (allowed/blocked) with recent events
- command surfaces compact policy history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T92 - Posture Action Policy Metrics
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T91

Deliverables:
- posture action policy metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/metrics`)
- intent command (`show continuity autopilot posture actions policy metrics`)
- windowed allowed/blocked policy metrics with policy-code/reason counters

Acceptance:
- endpoint returns summary (`allowed/blocked` counts and percentages) plus policy code/reason maps
- command surfaces compact policy metrics lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T93 - Posture Action Policy Anomaly Surface
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T92

Deliverables:
- posture action policy anomaly endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies`)
- intent command (`show continuity autopilot posture actions policy anomalies`)
- anomaly detection over policy-decision history (blocked streaks, repeated policy codes, blocked code events)

Acceptance:
- endpoint returns anomaly summary/counts and recent anomaly items
- command surfaces compact policy anomaly lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T94 - Posture Action Policy Anomaly Metrics
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T93

Deliverables:
- posture action policy anomaly metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/metrics`)
- intent command (`show continuity autopilot posture actions policy anomalies metrics`)
- windowed anomaly-rate metrics for posture action policy decisions

Acceptance:
- endpoint returns summary (`count`, `anomalies`, `anomalyRatePct`) plus anomaly-type/policy-code counters
- command surfaces compact anomaly-metrics lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T95 - Posture Action Policy Anomaly History
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T94

Deliverables:
- posture action policy anomaly history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/history`)
- intent command (`show continuity autopilot posture actions policy anomalies history`)
- compact anomaly-history surface (recent anomaly events + type counts)

Acceptance:
- endpoint returns summary/counts and recent anomaly history items
- command surfaces anomaly history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T96 - Posture Action Policy Anomaly Trend
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T95

Deliverables:
- posture action policy anomaly trend endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/trend`)
- intent command (`show continuity autopilot posture actions policy anomalies trend`)
- bucketed anomaly-rate trend output across a bounded window

Acceptance:
- endpoint returns trend summary (`count`, `anomalies`, `anomalyRatePct`, `trend`) and bucket series
- command surfaces compact trend lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T97 - Posture Action Policy Anomaly Offenders
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T96

Deliverables:
- posture action policy anomaly offenders endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/offenders`)
- intent command (`show continuity autopilot posture actions policy anomalies offenders`)
- ranked offender output (top commands contributing to policy anomalies)

Acceptance:
- endpoint returns summary + offender list over a bounded window
- command surfaces compact offender lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T98 - Posture Action Policy Anomaly State
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T97

Deliverables:
- posture action policy anomaly state endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/state`)
- intent command (`show continuity autopilot posture actions policy anomalies state`)
- consolidated anomaly health snapshot (health, trend, anomaly rate, top code/offender)

Acceptance:
- endpoint returns summary health plus embedded metrics/trend/offender snapshots
- command surfaces compact anomaly-state lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T99 - Posture Action Policy Anomaly Budget
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T98

Deliverables:
- posture action policy anomaly budget endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget`)
- intent command (`show continuity autopilot posture actions policy anomalies budget`)
- threshold-based anomaly budget summary (`within_budget|exceeded`, severity, remaining pct)

Acceptance:
- endpoint returns summary with threshold, rate, status/severity, and counts
- command surfaces compact budget status lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T100 - Posture Action Policy Anomaly Budget Breaches
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T99

Deliverables:
- posture action policy anomaly budget breaches endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/breaches`)
- intent command (`show continuity autopilot posture actions policy anomalies budget breaches`)
- bucketed breach surface showing threshold exceedance by time bucket

Acceptance:
- endpoint returns summary (`breachCount`, `topOverPct`) plus breach items
- command surfaces compact breach lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T101 - Posture Action Policy Anomaly Budget Forecast
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T100

Deliverables:
- posture action policy anomaly budget forecast endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast`)
- projected anomaly-rate budget status derived from recent trend slope

Acceptance:
- endpoint returns summary with threshold/current/slope/projected rate and projected status/risk
- command surfaces compact forecast lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T102 - Posture Action Policy Anomaly Budget Forecast Matrix
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T101

Deliverables:
- posture action policy anomaly budget forecast matrix endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/matrix`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast matrix`)
- multi-threshold forecast matrix (20/35/50%) with projected status/risk per row

Acceptance:
- endpoint returns summary (`rows`, `topRisk`) and matrix items
- command surfaces compact matrix lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T103 - Posture Action Policy Anomaly Budget Forecast Guidance
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T102

Deliverables:
- posture action policy anomaly budget forecast guidance endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance`)
- synthesized guidance over forecast matrix (`recommendation`, `reason`, `targetThresholdPct`)

Acceptance:
- endpoint returns guidance summary plus backing matrix payload
- command surfaces compact guidance lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T104 - Posture Action Policy Anomaly Budget Guidance Actions
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T103

Deliverables:
- guidance-actions endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions`)
- actionable command list derived from guidance recommendation and target threshold

Acceptance:
- endpoint returns summary and ordered action list
- command surfaces compact action lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T105 - Guidance Action Dry-Run
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T104

Deliverables:
- guidance-action dry-run endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/dry-run`)
- intent command (`dry run continuity autopilot posture actions policy anomalies budget forecast guidance action`)
- dry-run preview for selected guidance action (mapped op, capability, policy, appliable)

Acceptance:
- endpoint returns selected action plus mapped operation/policy evaluation
- command surfaces compact dry-run readiness lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T106 - Guidance Action Apply
Status: done
Track: D/E (continuity + operator execution)
Priority: P1
Dependencies: T105

Deliverables:
- guidance-action apply endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/apply`)
- intent command (`apply continuity autopilot posture actions policy anomalies budget forecast guidance action`)
- policy-checked execution of selected guidance action with apply report payload

Acceptance:
- endpoint returns apply report (`applied`, `reason`, `message`) for selected action
- command surfaces compact apply result lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T107 - Guidance Actions Apply-Batch
Status: done
Track: D/E (continuity + operator execution)
Priority: P1
Dependencies: T106

Deliverables:
- guidance-actions apply-batch endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/apply-batch`)
- intent command (`apply continuity autopilot posture actions policy anomalies budget forecast guidance actions`)
- bounded batch execution for top guidance actions with per-item apply status

Acceptance:
- endpoint returns batch report (`attempted`, `applied`, `changed`, `items`)
- command surfaces compact batch apply lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T108 - Guidance Actions History
Status: done
Track: D/E (continuity + operator auditability)
Priority: P1
Dependencies: T107

Deliverables:
- guidance-actions history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/history`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions history`)
- compact audit surface over guidance action apply/apply-batch journal events

Acceptance:
- endpoint returns summary (`count`, `applied`, `failed`) and recent history items
- command surfaces compact guidance-history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T109 - Guidance Actions Metrics
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T108

Deliverables:
- guidance-actions metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/metrics`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions metrics`)
- windowed metrics over guidance action execution journal events

Acceptance:
- endpoint returns summary (`count`, `applied`, `failed`, `appliedPct`, `topOp`) plus op counters
- command surfaces compact metrics lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T110 - Guidance Actions Anomalies
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T109

Deliverables:
- guidance-actions anomalies endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies`)
- anomaly detection over guidance action execution history (failure streaks + repeated failure reasons)

Acceptance:
- endpoint returns summary/counts and recent anomaly items
- command surfaces compact anomaly lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T111 - Guidance Actions Anomalies Trend
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T110

Deliverables:
- guidance-actions anomalies trend endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/trend`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies trend`)
- bucketed anomaly-rate trend for guidance action execution timeline

Acceptance:
- endpoint returns trend summary (`count`, `anomalies`, `anomalyRatePct`, `trend`) and bucket series
- command surfaces compact trend lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T112 - Guidance Actions Anomalies State
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T111

Deliverables:
- guidance-actions anomalies state endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/state`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies state`)
- consolidated anomaly state surface (health, trend, rates, top anomaly type)

Acceptance:
- endpoint returns summary health plus embedded metrics/anomaly/trend summaries
- command surfaces compact anomaly-state lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T113 - Guidance Actions Anomalies Offenders
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T112

Deliverables:
- guidance-actions anomalies offenders endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/offenders`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies offenders`)
- offender ranking for repeated guidance-action failure reasons (with top failing operation)

Acceptance:
- endpoint returns summary (`count`, `offenderCount`, `topOp`) and offender list
- command surfaces compact offender lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T114 - Guidance Actions Anomalies Timeline
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T113

Deliverables:
- guidance-actions anomalies timeline endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/timeline`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies timeline`)
- compact timeline of recent failing guidance-action events with timestamp/reason/op

Acceptance:
- endpoint returns summary (`count`, `latestTs`) and timeline items
- command surfaces compact timeline lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T115 - Guidance Actions Anomalies Summary
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T114

Deliverables:
- guidance-actions anomalies summary endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/summary`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies summary`)
- consolidated summary view combining state, top offenders, and recent timeline

Acceptance:
- endpoint returns summary (`health`, `trend`, `anomalyRatePct`, `topOp`, `topReason`, `latestTs`) and compact offender/timeline sections
- command surfaces compact summary lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T116 - Guidance Actions Anomalies Matrix
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T115

Deliverables:
- guidance-actions anomalies matrix endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/matrix`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies matrix`)
- hotspot matrix for failure reason x guidance op counts

Acceptance:
- endpoint returns summary (`count`, `reasons`, `ops`, `topReason`, `topOp`) plus row/column matrix slices
- command surfaces compact matrix summary lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T117 - Guidance Actions Anomalies Remediation
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T116

Deliverables:
- guidance-actions anomalies remediation endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`)
- reason-aware remediation suggestions mapped to concrete operator commands

Acceptance:
- endpoint returns summary (`count`, `offenderCount`, `topOp`, `suggestionCount`) and suggestion items
- command surfaces compact remediation lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T118 - Guidance Actions Anomalies Remediation Dry-Run
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T117

Deliverables:
- remediation dry-run endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/dry-run`)
- intent command (`dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`)
- selected remediation command preview with parsed type/domain/risk and executable check

Acceptance:
- endpoint returns summary (`selectedReason`, `selectedCommand`, `selectedType`, `knownCommand`) and dry-run fields (`domain`, `risk`, `canExecute`)
- command surfaces compact dry-run preview lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T119 - Guidance Actions Anomalies Remediation Apply
Status: done
Track: D/E (continuity + operator execution)
Priority: P1
Dependencies: T118

Deliverables:
- remediation apply endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/apply`)
- intent command (`apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation`)
- policy-checked execution of selected remediation command with apply report (`applied`, `changed`, `reason`, `selectedCommand`)

Acceptance:
- endpoint returns apply report including selected command/type and policy/capability context
- command surfaces compact apply-result lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T120 - Guidance Actions Anomalies Remediation History
Status: done
Track: D/E (continuity + operator auditability)
Priority: P1
Dependencies: T119

Deliverables:
- remediation history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/history`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation history`)
- compact audit history over remediation apply events

Acceptance:
- endpoint returns summary (`count`, `applied`, `failed`) and recent history items
- command surfaces compact remediation-history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T121 - Guidance Actions Anomalies Remediation Metrics
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T120

Deliverables:
- remediation metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/metrics`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation metrics`)
- windowed metrics over remediation apply events (count/applied/failed/appliedPct/topPolicyCode)

Acceptance:
- endpoint returns summary plus policy/type counters
- command surfaces compact remediation-metrics lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T122 - Guidance Actions Anomalies Remediation State
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T121

Deliverables:
- remediation state endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/state`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation state`)
- consolidated remediation state summary (health/trend plus applied/failure posture)

Acceptance:
- endpoint returns summary (`health`, `trend`, `count`, `applied`, `failed`, `appliedPct`, `topPolicyCode`)
- command surfaces compact remediation-state lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T123 - Guidance Actions Anomalies Remediation Trend
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T122

Deliverables:
- remediation trend endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/trend`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation trend`)
- bucketed remediation apply success/failure trend over window

Acceptance:
- endpoint returns summary (`count`, `applied`, `failed`, `appliedPct`, `trend`) and bucket series
- command surfaces compact remediation-trend lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T124 - Guidance Actions Anomalies Remediation Offenders
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T123

Deliverables:
- remediation offenders endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/offenders`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation offenders`)
- offender ranking over failed remediation apply events (policy code offenders + top selected type)

Acceptance:
- endpoint returns summary (`count`, `offenderCount`, `topType`) and offender list
- command surfaces compact remediation-offender lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T125 - Guidance Actions Anomalies Remediation Summary
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T124

Deliverables:
- remediation summary endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/summary`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation summary`)
- consolidated summary across remediation metrics/state/trend/offenders

Acceptance:
- endpoint returns summary (`health`, `trend`, `count`, `applied`, `failed`, `appliedPct`, `topPolicyCode`, `topType`, `offenderCount`) and compact linked sections
- command surfaces compact remediation-summary lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T126 - Guidance Actions Anomalies Remediation Timeline
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T125

Deliverables:
- remediation timeline endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/timeline`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation timeline`)
- compact remediation apply timeline (timestamp/outcome/policyCode/selectedType)

Acceptance:
- endpoint returns summary (`count`, `latestTs`) and timeline items
- command surfaces compact remediation-timeline lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T127 - Guidance Actions Anomalies Remediation Matrix
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T126

Deliverables:
- remediation matrix endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/matrix`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation matrix`)
- hotspot matrix over remediation apply events (`policyCode x selectedType`)

Acceptance:
- endpoint returns summary (`count`, `policyCodes`, `selectedTypes`, `topPolicyCode`, `topType`) plus row/column slices
- command surfaces compact remediation-matrix lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T128 - Guidance Actions Anomalies Remediation Guidance
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T127

Deliverables:
- remediation guidance endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance`)
- prioritized guidance recommendations derived from remediation state/trend/offenders

Acceptance:
- endpoint returns summary (`health`, `trend`, `topPolicyCode`, `offenderCount`, `guidanceCount`) and guidance items
- command surfaces compact remediation-guidance lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T129 - Guidance Actions Anomalies Remediation Guidance Actions
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T128

Deliverables:
- remediation guidance-actions endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions`)
- indexed actionable command list derived from remediation guidance recommendations

Acceptance:
- endpoint returns summary (with `actionCount`) and `actions[]` list including `index`, `priority`, `reason`, `command`
- command surfaces compact indexed action lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T130 - Guidance Actions Anomalies Remediation Guidance Action Dry-Run
Status: done
Track: D/E (continuity + operator safety)
Priority: P1
Dependencies: T129

Deliverables:
- remediation guidance-action dry-run endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/dry-run`)
- intent command (`dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action`)
- dry-run preview for selected remediation guidance action (mapped op, capability, policy, appliable)

Acceptance:
- endpoint returns selected action plus mapped operation/policy evaluation
- command surfaces compact dry-run readiness lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T131 - Guidance Actions Anomalies Remediation Guidance Action Apply
Status: done
Track: D/E (continuity + operator execution)
Priority: P1
Dependencies: T130

Deliverables:
- remediation guidance-action apply endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply`)
- intent command (`apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action`)
- policy-checked execution for selected remediation guidance action with apply report (`applied`, `changed`, `reason`, `selectedCommand`, `selectedType`)

Acceptance:
- endpoint returns apply report including selected command/type with policy + capability context
- command surfaces compact apply result lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T132 - Guidance Actions Anomalies Remediation Guidance Actions Apply-Batch
Status: done
Track: D/E (continuity + operator execution)
Priority: P1
Dependencies: T131

Deliverables:
- remediation guidance-actions apply-batch endpoint (`POST /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply-batch`)
- intent command (`apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions`)
- batch apply report with per-item outcomes (`attempted`, `applied`, `changed`, `items[]`)

Acceptance:
- endpoint returns report with aggregate and per-index apply status
- command surfaces compact batch outcome lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T133 - Guidance Actions Anomalies Remediation Guidance Actions History
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T132

Deliverables:
- remediation guidance-actions history endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/history`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions history`)
- compact audit history over guidance-action apply/apply-batch executions

Acceptance:
- endpoint returns summary (`count`, `applied`, `failed`) and recent history items
- command surfaces compact guidance-actions history lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T134 - Guidance Actions Anomalies Remediation Guidance Actions Metrics
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T133

Deliverables:
- remediation guidance-actions metrics endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/metrics`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions metrics`)
- windowed metrics over guidance-action execution (`count`, `applied`, `failed`, batch attempt/apply percentages)

Acceptance:
- endpoint returns summary plus op/policy counters
- command surfaces compact guidance-actions metrics lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T135 - Guidance Actions Anomalies Remediation Guidance Actions State
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T134

Deliverables:
- remediation guidance-actions state endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/state`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions state`)
- consolidated state summary combining guidance-actions metrics/history (`health`, `trend`, `appliedPct`, `topPolicyCode`)

Acceptance:
- endpoint returns summary health/trend plus linked metrics/history summaries
- command surfaces compact guidance-actions state lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T136 - Guidance Actions Anomalies Remediation Guidance Actions Trend
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T135

Deliverables:
- remediation guidance-actions trend endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/trend`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions trend`)
- bucketed trend summary over guidance-action execution (`count`, `applied`, `failed`, `appliedPct`, `trend`)

Acceptance:
- endpoint returns summary and trend series buckets
- command surfaces compact guidance-actions trend lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T137 - Guidance Actions Anomalies Remediation Guidance Actions Offenders
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T136

Deliverables:
- remediation guidance-actions offenders endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/offenders`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions offenders`)
- offender ranking for repeated failed guidance-action policy codes

Acceptance:
- endpoint returns summary (`count`, `offenderCount`, `topOp`) and offenders list
- command surfaces compact guidance-actions offender lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T138 - Guidance Actions Anomalies Remediation Guidance Actions Summary
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T137

Deliverables:
- remediation guidance-actions summary endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/summary`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions summary`)
- consolidated summary across state/trend/offenders for guidance-action execution

Acceptance:
- endpoint returns summary (`health`, `trend`, `count`, `applied`, `failed`, `appliedPct`, `topPolicyCode`, `offenderCount`) and compact offender section
- command surfaces compact guidance-actions summary lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T139 - Guidance Actions Anomalies Remediation Guidance Actions Timeline
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T138

Deliverables:
- remediation guidance-actions timeline endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/timeline`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions timeline`)
- compact timeline view over guidance-action executions (`ts`, `ok`, `op`, policy context)

Acceptance:
- endpoint returns summary (`count`, `latestTs`) and timeline items
- command surfaces compact guidance-actions timeline lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T140 - Guidance Actions Anomalies Remediation Guidance Actions Matrix
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T139

Deliverables:
- remediation guidance-actions matrix endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/matrix`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions matrix`)
- hotspot matrix over guidance-action execution (`policyCode x selectedType`)

Acceptance:
- endpoint returns summary (`count`, `policyCodes`, `selectedTypes`, `topPolicyCode`, `topType`) plus row/column and matrix slices
- command surfaces compact guidance-actions matrix lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T141 - Guidance Actions Anomalies Remediation Guidance Actions Guidance
Status: done
Track: D/E (continuity + operator observability)
Priority: P1
Dependencies: T140

Deliverables:
- remediation guidance-actions guidance endpoint (`GET /api/session/{id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/guidance`)
- intent command (`show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions guidance`)
- prioritized guidance recommendations derived from guidance-actions summary/state signals

Acceptance:
- endpoint returns summary (`health`, `trend`, `topPolicyCode`, `guidanceCount`) and guidance items
- command surfaces compact guidance-actions guidance lines in operator feed
- replay/UI tests validate endpoint and command behavior

Notes:
- backend implementation in `backend/main.py`
- replay coverage in `scripts/replay_smoke.py`
- UI coverage in `tests/ui/operator.spec.js`
- docs update in `README.md`

---

## T142 - UI Test Harness Stability + Flake Elimination
Status: done
Track: E (packaging/devex/ops)
Priority: P1
Dependencies: T141

Deliverables:
- stabilize Playwright runner for local-first CI-like execution (serial worker mode + retries)
- remove fragile UI feed-text dependency in long operator flow by asserting intent-plane API outcomes
- harden handoff test against transient feed-command rendering by seeding handoff token via API
- normalize transient status waits in jobs/trace flows to handle fast local turns

Acceptance:
- `npm run ui:test` passes locally without manual retries
- `npm run os:test:replay` and `npm run os:test:handoff` remain green after UI harness changes
- test suite no longer fails on `ECONNRESET/ECONNREFUSED` caused by concurrent Playwright runner contention

Notes:
- updated `playwright.config.js` (`workers: 1`, retries, timeout)
- updated `tests/ui/operator.spec.js`, `tests/ui/handoff.spec.js`, `tests/ui/jobs.spec.js`, `tests/ui/trace.spec.js`
- validated with `npm run ui:test`, `npm run os:test:replay`, `npm run os:test:handoff`

## Suggested Execution Order
1. T3
2. T4
3. T5
4. T6
5. T7
6. T8
