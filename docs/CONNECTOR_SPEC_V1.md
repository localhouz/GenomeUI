# Connector Spec v1 (Everyday Domains)

## Purpose
Define how GenomeUI connects everyday user intents (weather, calls, banking, social, web) to real services without turning back into an app-switching OS.

Core rule:
- intent-first UX
- connector-based execution
- local-first policy and audit
- explicit user consent for all external access

## Design Goals
- Familiar outcomes for basic users.
- No raw API/app jargon in the primary surface.
- Deterministic safety gates on all side effects.
- No broker-style third-party tracking dependencies.
- Connector actions are explainable, replayable, and reversible where possible.

## Connector Abstraction
Each connector implements:
- `manifest`: connector id, version, provider, scopes, risk map
- `capabilities`: verbs + object schemas
- `device_profiles`: desktop/mobile support matrix + fallback mode
- `auth`: token/session strategy + refresh strategy
- `policy_hooks`: per-operation allow/deny/confirm checks
- `exec`: deterministic operation handlers
- `normalize`: map provider payloads to GenomeUI canonical objects
- `audit`: append execution records to journal/trace

## Domain Set (v1)
1. Weather
- Read-only forecast/current conditions.
- Sources: trusted weather APIs.
- No background writes.

2. Calls
- Initiate call through device telephony bridge (or configured VoIP provider).
- Must require explicit confirm before dialing.

3. Banking
- Read-first (balances, recent transactions) in v1.
- Payment/transfer actions behind high-risk confirmation in later phase.
- Prefer direct bank OAuth/aggregator with user-owned session.

4. Social/Web
- Explicit web session connectors (Instagram/site access through user auth).
- Read and navigation in v1; write actions gated.
- Treat social writes as medium/high risk.

5. General Web
- Query/fetch + summarize with domain allowlist/denylist.
- No localhost/private network targets.

## Device Profiles (Desktop vs Mobile)
Each connector operation must declare:
- `desktop`: `full | partial | unsupported`
- `mobile`: `full | partial | unsupported`
- `fallback`: behavior when unsupported/degraded

Fallback examples:
- `telephony.call.start`:
  - mobile: `full` (native dial bridge + confirm)
  - desktop: `partial` (handoff call to paired phone)
- `bank.payment.send`:
  - mobile/desktop: `partial` in v1 (read + prepare, confirm in bank flow)
- `file.system.write`:
  - desktop: `full`
  - mobile: `partial` (sandbox paths/providers only)

## Mobile-First Constraints
- Small-screen output must default to concise cards + action chips.
- Destructive/high-risk actions require explicit confirm regardless of device.
- No assumptions about background execution on mobile.
- Connector polling/rates must use mobile battery/network budgets.
- If a capability is desktop-only, surface a clear handoff option:
  - "Continue on desktop" with intent/token continuity.

## Mobile Everyday Capability Baseline (v1)
Must work on phone:
- weather read
- web read + summarize
- calls (dial with confirm)
- contacts lookup
- reminders/tasks basic mutate
- banking read (balances + recent transactions)

Allowed to be desktop-first in v1:
- deep file system operations
- large-batch document transforms
- advanced operator diagnostics

## Permission Model
Scope format:
- `domain.resource.verb`
- examples:
  - `weather.forecast.read`
  - `telephony.call.start`
  - `bank.account.balance.read`
  - `social.instagram.feed.read`
  - `social.instagram.message.send`

Grant model:
- per-connector grant, per-scope grant, optional TTL
- least-privilege defaults
- explicit revoke in settings/intent plane

Risk mapping:
- `low`: read operations
- `medium`: external mutations (post/send)
- `high`: money movement / calls / destructive actions

Policy outcomes:
- `allowed`
- `denied`
- `confirmation_required`
- `rate_limited`

## Auth and Secret Handling
Auth modes:
- OAuth2 (preferred where available)
- session-cookie bridge (user-owned browser session, local capture with consent)
- API key (weather-class providers only)

Secret storage:
- local encrypted vault
- never logged in plain text
- never sent to planner/model payloads

Token handling:
- refresh in connector runtime only
- short-lived access tokens
- hard fail closed on refresh/auth errors

## Data Contracts (Canonical)
Weather object:
- `provider`, `location`, `current`, `hourly[]`, `daily[]`, `fetchedAt`

Call intent/result:
- `target`, `provider`, `mode`, `status`, `startedAt`, `endedAt`

Account snapshot:
- `institution`, `accountIdMasked`, `accountType`, `balance`, `currency`, `asOf`

Transaction:
- `id`, `postedAt`, `description`, `amount`, `currency`, `category`, `pending`

Social item:
- `source`, `entityType`, `id`, `text`, `author`, `createdAt`, `url`

## Execution Model
1. Intent classified to connector op.
2. Capability resolution and scope check.
3. Policy evaluation (`allowed/confirm/deny`).
4. Connector execution (or blocked with next-step prompt).
5. Canonical normalization into world model.
6. Trace + audit append:
   - connector id/version
   - op + scope
   - policy decision
   - side-effect summary
   - latency/error code

## UX Rules for Basic Users
- Never surface raw connector ids in primary view.
- Show outcome statements:
  - "Current weather in Austin: 67F, light rain."
  - "Ready to call Mike. Confirm to dial."
  - "Checking account balance: $2,431.12."
- Present action chips over command jargon.
- Keep technical details in activity feed/audit, not main headline.

## Privacy and Tracking Policy
- No ad-tech SDK connectors.
- No cross-service identity graphing.
- No passive data sync without explicit grant.
- Per-connector telemetry is opt-in and minimal.
- Redact personally sensitive fields in analytics.

## Failure Modes and Safe Fallbacks
- Auth expired: show reconnect action, no silent retries with side effects.
- Provider down: degrade gracefully, keep local state intact.
- Scope missing: ask for just-in-time permission.
- Risk blocked: return explicit confirm command/chip.

## Rollout Plan
Phase A (now):
- Weather read connector
- Web read connector hardening
- Intent label cleanup in UI (plain language outcomes)
- Device profile schema + mobile fallback rules

Phase B:
- Telephony call-start connector with confirmation
- Banking read connector (balances + recent transactions)
- Mobile handoff action for desktop-only intents

Phase C:
- Social read connector (Instagram/web session)
- Limited social write connector with confirmation and rate limits
- Mobile UX pass for concise connector outcomes/chips

Phase D:
- Banking write workflows (bill pay/transfer) with high-risk policy templates

## Acceptance Criteria (v1)
- Basic user can complete:
  - check weather
  - initiate a call with confirmation
  - check bank balance
  - read Instagram/web feed
- On mobile, unsupported operations return explicit fallback/handoff actions (not silent failure).
- Each operation has clear policy/audit trace.
- No raw provider secrets appear in logs/traces.
- Connector failures are understandable and recoverable.

## Immediate Ticket Seeds
- `C1`: Connector manifest schema + registry
- `C2`: Vault abstraction for connector secrets
- `C3`: Weather connector (read)
- `C4`: Telephony bridge connector (call start + confirm)
- `C5`: Banking read connector
- `C6`: Social read connector
- `C7`: Plain-language outcome renderer for connector responses
- `C8`: Connector permission management surface
- `C9`: Connector test harness (mock providers + replay)
- `C10`: Connector security review checklist
- `C11`: Device profile schema + enforcement (`desktop/mobile/fallback`)
- `C12`: Mobile connector UX contract (cards/chips/handoff)
