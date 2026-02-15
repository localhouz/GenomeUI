# GenomeUI Generative OS Build Plan

## Goal
Build a local-first generative operating system where intent compiles into safe capability execution and the interface is synthesized from live state, across desktop and phone, with minimal latency.

## Product Definition
A Generative OS in this project means:
- intent-first control plane, not app-first navigation
- capability kernel executes actions under policy
- unified world model (versioned object graph)
- continuous runtime (agents, scheduler, event bus)
- generated surface from schema-constrained primitives
- seamless cross-device continuity for a shared session

## Current Baseline
- Frontend shell + command bar + right activity feed
- Python backend with turn processing and WS/SSE sync
- Deterministic planner + optional Ollama route
- Single launcher and venv-first local runtime

## Build Phases

## Phase 0: Stabilize Foundation
Status: in progress
Objective: eliminate UI/runtime friction and lock a reliable dev/test loop.

Deliverables:
- no nested/inner scroll artifacts in main shell
- stable launcher defaults (no noisy WS resets)
- reproducible startup checks (`os:test`)
- clearer logs for backend route/planner selection

Acceptance gates:
- cold boot to ready in <= 2.5s on dev machine
- no fatal startup errors in 20 consecutive launches
- cross-device session sync verified on WS and fallback paths

---

## Phase 1: Capability Kernel + Policy Runtime
Objective: make intent execution OS-like, safe, and auditable.

Deliverables:
- capability registry (`tasks`, `notes`, `expenses`, `files`, `web`, `system`)
- policy engine (allow/deny/confirm/rate-limit)
- action journal with reversible ops where possible
- risk classes (`low`, `medium`, `high`) with confirmation gates

Acceptance gates:
- every mutation is logged with actor/session/timestamp/diff
- denied actions never execute side effects
- high-risk actions always require explicit confirmation

---

## Phase 2: World Model + Memory Graph
Objective: move from siloed arrays to a unified object graph.

Deliverables:
- normalized entity graph (objects, relations, events)
- revisioned snapshots and conflict resolution strategy
- query layer for planner and UI generation
- schema contracts for object types and relationships

Acceptance gates:
- all capabilities read/write via graph API
- state rehydration reproduces prior UI surface deterministically
- multi-device edits converge without data loss

---

## Phase 3: Planner Stack (Model-by-Intent)
Objective: route intents to the right planner/model/toolchain.

Deliverables:
- intent classifier (query, mutate, automate, risky, long-horizon)
- route policy (deterministic vs small model vs large model)
- confidence thresholds + clarification loop
- planner trace object persisted per turn

Acceptance gates:
- route choice explainable for every turn
- low-confidence intents trigger clarification instead of bad execution
- deterministic fallback always available offline

---

## Phase 4: Continuous Runtime (Event Bus + Scheduler + Agents)
Objective: make goals run over time, not only per prompt.

Deliverables:
- event bus and task scheduler
- background workers/agents with retry + dead-letter queue
- watch triggers (time, file, state-change)
- idempotent execution keys for repeat safety

Acceptance gates:
- scheduled intents survive restart
- retries bounded and observable
- failed jobs visible and replayable

---

## Phase 5: Generative Surface Engine v2
Objective: remove dashboard assumptions and render from synthesis contracts.

Deliverables:
- UI synthesis schema (planes, slots, priorities, density)
- primitive renderer (text, list, stream, action, status)
- adaptive layout for desktop/phone continuity
- feed-first operation trace (what changed and why)

Acceptance gates:
- no hardcoded domain-specific dashboard templates
- same intent + same graph state => same surface output
- surface remains readable on mobile without separate app mode

---

## Phase 6: OS Interaction Model + Handoff
Objective: make desktop/phone feel like one continuous system.

Deliverables:
- presence model (active device, handoff token, latency budget)
- session handoff protocol (pointer + focus + pending actions)
- command plane shortcuts and universal activity timeline

Acceptance gates:
- handoff in <= 500ms perceived continuity in local network tests
- no duplicated actions during handoff
- shared session state remains consistent after 100 handoffs

---

## Phase 7: Packaging, Local Runtime, and Install
Objective: single-command install/run for local users.

Deliverables:
- one launcher with bootstrap, health checks, diagnostics
- local model discovery and profile selection
- optional offline profile with deterministic mode only
- crash recovery and startup repair path

Acceptance gates:
- first-time setup <= 10 minutes on clean Windows machine
- launcher can self-diagnose missing deps and suggest fixes
- deterministic mode works fully without cloud APIs

---

## Phase 8: Observability + Eval Harness
Objective: measure quality and prevent regressions.

Deliverables:
- end-to-end turn trace (`intent -> route -> plan -> actions -> diff -> UI`)
- replay suite for canonical intents
- latency/correctness/safety scorecards
- regression CI checks

Acceptance gates:
- replay suite deterministic for local mode
- release blocked on safety and correctness thresholds
- performance baseline tracked per commit

## Engineering Tracks (Parallel)
- Track A: Runtime/kernel/policy
- Track B: Planner/router/evals
- Track C: Surface synthesis/interaction UX
- Track D: Device continuity/networking
- Track E: Packaging/devex/ops

## Priority Order (Recommended)
1. Phase 1 (Capability + Policy)
2. Phase 2 (World Model)
3. Phase 3 (Model-by-Intent Router)
4. Phase 5 (Generative Surface v2)
5. Phase 4 (Continuous Runtime)
6. Phase 6 (Handoff)
7. Phase 7 (Packaging)
8. Phase 8 (Eval/CI hardening)

## Immediate 2-Week Sprint
Sprint objective: complete first usable OS core loop.

Scope:
- implement capability registry and policy middleware
- add action journal + diff recorder
- migrate current task/expense/note ops to kernel path
- add confirmation flow for risky operations
- expose trace in activity feed (`route`, `policy`, `result`)

Definition of done:
- all mutations pass through kernel + policy
- every turn has trace and diff
- no direct state mutation from planner layer
- smoke + replay tests pass locally

## Risks and Mitigations
- Risk: UI keeps drifting into dashboard patterns
  - Mitigation: enforce primitive-only renderer and schema contracts
- Risk: latency from over-routing to heavy models
  - Mitigation: route budget + deterministic defaults + cached plans
- Risk: unsafe automation side effects
  - Mitigation: policy gates, confirmations, scoped capabilities
- Risk: sync divergence across devices
  - Mitigation: revisioned graph, conflict rules, replayable event log

## Change Control
- each phase starts with RFC in `/docs/rfcs`
- each phase ends with acceptance checklist and demo script
- no new surface features without traceability to capability + policy

