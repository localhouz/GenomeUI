# GenomeUI — Generative OS Build Plan

> **Rule:** Every ticket must be fully built before moving to the next. No stubs. No "wire later."
> If a ticket says "live data," that means real API calls, not mock returns.
> If a ticket says "Windows Service," that means a registered Scheduled Task, not a supervisor script.

---

## Tier 0 — OS Foundation

*These are not features. They are the floor. Without these, GenomeUI is a demo, not an OS.*

---

### T0-1 — Process Supervision as Windows Services

**Status:** IMPLEMENTED IN REPO
**Priority:** FIRST — blocks everything else

**What it is:**
Nous + Backend registered as Windows Scheduled Task services that boot at logon,
run whether Electron is open or not, and auto-restart on crash via Windows recovery policy.
`dev.ps1` becomes: "ensure services running → start Vite → start Electron."

**Acceptance Criteria:**
- [ ] `scripts/install-services.ps1` creates two Scheduled Tasks: `GenomeUI-Nous` and `GenomeUI-Backend`
- [ ] Tasks trigger at logon, run as current user, restart on failure (up to 10 times, 1s delay)
- [ ] `scripts/uninstall-services.ps1` removes both tasks cleanly
- [ ] `dev.ps1` checks if services are running before starting Vite/Electron — starts them if not
- [ ] Electron close no longer kills backend or Nous
- [ ] Backend and Nous survive terminal close, OS sleep, and user logoff/logon
- [ ] Install script emits clear success/failure for each task registration
- [ ] README-SERVICES.md documents how to install, verify, and uninstall

**Files:**
- `scripts/install-services.ps1` (new)
- `scripts/uninstall-services.ps1` (new)
- `scripts/dev.ps1` (modify: remove supervisor loop, add service-check prologue)

**Notes:**
- Use `Register-ScheduledTask` with `-RunLevel Highest` so services have correct permissions
- Nous binary path and Backend venv path must be absolute in task definition
- Recovery policy: `$settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Seconds 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0)`
- Task action for Backend: `$venvPython -m uvicorn backend.main:app --host 0.0.0.0 --port 8787`
- Task action for Nous: `nous-server.exe --port 7700 --model phi4-mini --genomeui http://localhost:8787`

---

### T0-2 — Session Persistence: SQLite

**Status:** IMPLEMENTED IN REPO
**Priority:** HIGH — sessions.json is a 74MB time bomb

**What it is:**
Replace the flat-file `sessions.json` with SQLite via `aiosqlite`. Atomic writes,
WAL mode, no full-file rewrite on every turn. Sessions survive backend restarts.

**Acceptance Criteria:**
- [ ] `backend/db.py` created with async SQLite connection pool (aiosqlite + WAL mode)
- [ ] Schema: `sessions(id TEXT PRIMARY KEY, state BLOB, revision INTEGER, updated_at INTEGER)`
- [ ] `load_session(session_id)` reads from DB, returns deserialized state dict
- [ ] `save_session(session_id, state)` writes atomically (no partial writes on crash)
- [ ] `list_sessions()` returns session IDs ordered by updated_at DESC
- [ ] `main.py` startup: opens DB, runs schema migration if tables missing
- [ ] `sessions.json` load path is kept as one-time migration: if file exists on boot, import all sessions to DB, then rename to `sessions.json.migrated`
- [ ] Backend restart test: create session, stop backend, restart, session is intact
- [ ] Write benchmark: 1000 session saves complete without I/O errors

**Files:**
- `backend/db.py` (new)
- `backend/main.py` (modify: replace JSON load/save with db.py calls)
- `requirements.txt` (add: aiosqlite)

---

### T0-3 — Nous Model Speed: <200ms Intent Classification

**Status:** IMPLEMENTED IN REPO, PERF TARGET STILL NEEDS BENCH VERIFICATION
**Priority:** HIGH — 3677ms parse makes Nous unusable

**What it is:**
The current phi4-mini model takes 3677ms to classify an intent. This is
10-20x too slow for an OS shell. Fix: switch to a model that can classify
in <200ms on CPU, OR route classification through the fine-tuned model
built in T1-1 (preferred long-term path).

**Acceptance Criteria:**
- [ ] `dev.ps1` default model changed to `qwen2.5-0.5b` (via Ollama — `ollama pull qwen2.5:0.5b`)
- [ ] End-to-end intent classification (user types → scene renders) completes in <500ms on dev hardware
- [ ] Nous gateway parse time logged on every request (`X-Nous-Parse-Ms` response header or backend log)
- [ ] `scripts/dev.ps1` --NousModel parameter documented with recommended values
- [ ] Fallback: if Nous response >1000ms, backend uses rule-based semantics.py result immediately
  (don't wait — fire Nous in background, update scene only if result differs)
- [ ] Model switch does not break any existing intent in the taxonomy test suite

**Files:**
- `scripts/dev.ps1` (modify: change default model flag)
- `backend/main.py` (modify: add Nous timeout fallback logic)
- `Nous/rust/` (if model config lives there)

---

### T0-4 — Clean Boot: Always Start at Latent Surface

**Status:** IMPLEMENTED IN REPO

**What it is:**
When the OS boots (Electron launches), the user sees the Latent Surface — not
the last rendered scene from a previous session. Session graph is preserved (history
is available) but the boot state is always neutral.

**Acceptance Criteria:**
- [ ] On app init, scene is always Latent Surface regardless of session graph state
- [ ] `app.js` boot sequence does NOT restore last rendered scene from session
- [ ] History reel (if visible) shows past sessions but does not auto-render them
- [ ] WebSocket reconnect after backend restart does NOT re-render last scene
- [ ] Manual test: render a scene, close Electron, reopen — Latent Surface loads

**Files:**
- `app.js` (modify: init sequence — clear scene state on boot)
- `electron/main.mjs` (verify: cache/SW clear already in place)

---

## Tier 1 — Intelligence

*Making Nous actually useful.*

---

### T1-1 — Fine-Tune Nous: General AI Assistant

**Status:** IMPLEMENTED IN REPO, TRAINING RUN / EVAL BENCH STILL PENDING
**Priority:** HIGH — transforms Nous from slow generalist to fast specialist

**What it is:**
Fine-tune Nous on the GenomeUI taxonomy + synthetically generated examples.
**Critical constraint:** the output model must be a general AI assistant usable on
any surface — not a hard-coded GenomeUI classifier. The fine-tuning teaches it the
GenomeUI intent vocabulary and reasoning style, but it must remain capable of
general conversation, code help, writing, and reasoning tasks.

**Acceptance Criteria:**
- [ ] Training dataset extended: `nous/dataset.jsonl` augmented with:
  - General conversation examples (question answering, coding help, writing)
  - All 355 taxonomy intents with 5-10 examples each
  - Multi-turn conversations showing agentic reasoning
  - "I don't know" / ambiguous intent examples (maps to `general.query`)
- [ ] `nous/train_modal.py` updated to fine-tune base model (not just classifier head)
  - Base model: Qwen2.5-0.5B-Instruct or Phi-3-Mini (chosen for <1GB size)
  - LoRA fine-tuning (not full fine-tune) — preserves general capability
  - Training platform: Modal.com (GPU, ~$10/run)
  - Output format: GGUF Q4_K_M (Ollama-compatible)
- [ ] Evaluation harness in `nous/eval.py`:
  - Intent classification accuracy on held-out taxonomy examples (target: >90%)
  - General capability: MMLU subset (target: within 5% of base model)
  - Latency on CPU: <200ms for classification tasks
- [ ] Trained model importable into Nous Rust gateway via Ollama modelfile
- [ ] `nous/README-TRAINING.md` documents: dataset format, training command, eval, deployment

**Files:**
- `nous/dataset.jsonl` (extend: general + per-intent examples)
- `nous/train_modal.py` (modify: LoRA fine-tune, not classifier-only)
- `nous/eval.py` (new: accuracy + latency benchmarks)
- `nous/README-TRAINING.md` (new)

---

### T1-2 — Nous Gateway Performance

**Status:** IMPLEMENTED IN REPO, LATENCY TARGETS STILL NEED BENCH VERIFICATION

**What it is:**
After model speed is fixed, ensure the full path (frontend → Nous → Backend → scene)
is instrumented and optimized. High-frequency intents bypass Nous entirely via
fast-path rule-based classification.

**Acceptance Criteria:**
- [ ] `backend/main.py` logs: `classify_ms`, `nous_ms`, `total_ms` per turn
- [ ] High-frequency intents (weather, time, music playback) resolved via semantics.py <5ms
- [ ] Nous invoked only for ambiguous or complex intents
- [ ] P95 latency <300ms for high-frequency intents
- [ ] P95 latency <500ms for Nous-routed intents (on dev hardware)

**Files:**
- `backend/main.py` (modify: timing instrumentation, fast-path routing)

---

### T1-3 — Agentic Multi-Step Reasoning

**Status:** IMPLEMENTED IN REPO, END-TO-END FLOW VERIFICATION STILL PENDING

**What it is:**
Today: one intent → one op → one scene.
Target: Nous returns a plan (sequence of ops), backend executes them in order,
scenes compose into a multi-panel result.

Example: "book a flight to NYC next Tuesday, add to calendar, message Sarah I'm coming"
→ Nous returns: `[travel.search, calendar.create, messaging.send]`
→ Backend executes each, returns composite scene

**Acceptance Criteria:**
- [x] Nous response schema extended: `plan: [{op, slots, depends_on}]` (existing `suggestions` field evolved)
- [x] Backend `run_plan()` executes op sequence, handles dependency ordering
- [x] Scene renderer supports composite layout (2-3 panels for multi-op results)
- [x] Error in step N does not block steps that don't depend on N
- [x] User can see which steps completed and which failed in the scene
- [x] At least 3 multi-step flows work end-to-end: travel+calendar, shopping+payment, message+reminder

**Files:**
- `backend/main.py` (new: `run_plan()`, plan schema)
- `app.js` (new: composite scene renderer)
- `Nous/rust/` (modify: plan output format)

---

## Tier 2 — Integration

*The OS is only as valuable as the services it surfaces. Mock data is not an OS.*

---

### T2-1 — Gmail Connector: Live Data

**Status:** IMPLEMENTED IN REPO, LIVE-CREDS VERIFICATION PENDING

**What it is:**
The Gmail connector exists as scaffold (OAuth flow defined, vault ready) but
`gmail_list_snapshot()` returns mock data. Wire it to the real Gmail API.

**Acceptance Criteria:**
- [ ] OAuth flow works end-to-end: user triggers Gmail auth → Google consent screen → callback → token stored in vault
- [ ] `gmail_list_snapshot()` calls Gmail API v1 `users.messages.list` + `users.messages.get`
- [ ] Returns real inbox: sender, subject, snippet, date, unread status, thread ID
- [ ] Token refresh implemented: if access token expired, refresh via refresh token automatically
- [ ] Email scene renderer in `app.js` binds to real data (no hardcoded names/subjects)
- [ ] Compose intent (`email.compose`) pre-fills scene with recipient/subject from intent slots
- [ ] Error state: if auth missing or API fails, scene shows "Connect Gmail" CTA, not broken layout
- [ ] Test: revoke token → re-auth flow works cleanly

**Files:**
- `backend/main.py` (modify: `gmail_list_snapshot()`, `gmail_send()`, token refresh)
- `app.js` (modify: email scene data binding)
- `.env.example` (document: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET required)

---

### T2-2 — Google Calendar Connector: Live Data

**Status:** IMPLEMENTED IN REPO, LIVE-CREDS VERIFICATION PENDING

**What it is:**
Same OAuth app as Gmail. `gcal_list()` returns mock. Wire to Google Calendar API.

**Acceptance Criteria:**
- [ ] `gcal_list()` calls Calendar API v3 `events.list` — real events, real times, real attendees
- [ ] `calendar.create` intent creates a real event via `events.insert`
- [ ] Calendar scene renderer binds to real events (week view with actual data)
- [ ] Token refresh shared with Gmail (same OAuth app, same refresh path)
- [ ] Cross-timezone events display correctly in user's local time
- [ ] Error state: "Connect Google Calendar" CTA if not authenticated

**Files:**
- `backend/main.py` (modify: `gcal_list()`, `gcal_create()`)
- `app.js` (modify: calendar scene data binding)

---

### T2-3 — Spotify Connector: Live Data

**Status:** IMPLEMENTED IN REPO, LIVE-CREDS VERIFICATION PENDING

**What it is:**
PKCE OAuth flow defined, scopes set. `spotify_now_playing_snapshot()` returns mock.
Wire to Spotify Web API.

**Acceptance Criteria:**
- [ ] PKCE OAuth flow works end-to-end (no client secret — PKCE only)
- [ ] `spotify_now_playing_snapshot()` returns real track: name, artist, album art URL, progress, duration
- [ ] `music.play`, `music.pause`, `music.skip` intents call Spotify Player API
- [ ] Music scene renderer binds to real track data (album art from real CDN URL)
- [ ] Playback control works: play/pause/next from GenomeUI scene reflects in Spotify app
- [ ] Error state: "Connect Spotify" CTA if not authenticated or no active device

**Files:**
- `backend/main.py` (modify: `spotify_now_playing_snapshot()`, `spotify_control()`)
- `app.js` (modify: music scene data binding)

---

### T2-4 — Connector Vault: Shared Token Refresh

**Status:** IMPLEMENTED IN REPO

**What it is:**
OAuth tokens expire. Every connector needs refresh logic. Implement once as a shared
helper so T2-1/2/3 don't each write their own.

**Acceptance Criteria:**
- [ ] `backend/auth.py` exports `async def refresh_token_if_needed(service: str) -> dict`
  - Loads tokens from vault
  - Checks expiry (with 60s buffer)
  - If expired: calls token refresh endpoint, stores new tokens in vault
  - Returns valid access token
- [ ] Gmail and Google Calendar use the same helper (same OAuth app)
- [ ] Spotify uses PKCE refresh path (no client secret)
- [ ] If refresh fails (revoked): raises `ConnectorAuthError` — caller shows "reconnect" CTA
- [ ] Token refresh is transparent to scene renderers — they never see expired tokens

**Files:**
- `backend/auth.py` (modify: add `refresh_token_if_needed()`)
- `backend/main.py` (modify: all connector functions use helper)

---

## Tier 3 — Distribution

*Getting to users.*

---

### T3-1 — Auto-Update

**Status:** IMPLEMENTED IN REPO

**What it is:**
Electron app checks for updates on launch, downloads in background, prompts user on next boot.

**Acceptance Criteria:**
- [x] `electron-updater` added to `package.json`
- [x] Update server configured (GitHub Releases preferred — free, no infrastructure)
- [x] On launch: silent update check, download if available
- [x] On next launch after download: prompt "Update ready — restart now?"
- [x] Update check failure does not block app launch
- [x] `electron-builder.yml` configured for auto-update (publishConfig)

**Files:**
- `package.json` (add: electron-updater)
- `electron/main.mjs` (add: autoUpdater setup)
- `electron-builder.yml` (add: publish config)

---

### T3-2 — Crash Reporting

**Status:** IMPLEMENTED IN REPO

**What it is:**
When the OS crashes, we need to know. Electron + Python both need handlers.

**Acceptance Criteria:**
- [x] Electron: `process.on('uncaughtException')` handler logs to file + optional POST to `/api/crash`
- [x] Electron renderer: `window.onerror` + `window.onunhandledrejection` captured
- [x] Python: `sys.excepthook` override in `main.py` writes crash to `backend.crash.log`
- [x] `/api/crash` endpoint in backend: appends to crash log, returns 200
- [x] Crash log includes: timestamp, process (electron/backend/nous), error message, stack trace
- [x] Crash log location documented in README

**Files:**
- `electron/main.mjs` (add: crash handlers)
- `backend/main.py` (add: `/api/crash` endpoint, `sys.excepthook`)

---

### T3-3 — Desktop Notifications

**Status:** IMPLEMENTED IN REPO

**What it is:**
Use Electron's `Notification` API (maps to Windows toast, macOS notification center).
Wire to reminders, incoming messages, and connector alerts.

**Acceptance Criteria:**
- [x] `electron/main.mjs` registers IPC handler: `ipcMain.handle('os:notify', handler)`
- [x] `app.js` calls `window.electronAPI.notify({title, body, icon?})` — works in Electron, no-ops in browser
- [x] Notifications fire for:
  - Reminder intent when timer expires
  - Incoming relay message (new message from contact)
  - Connector alert (calendar event starting in 5 min)
- [x] Notification click focuses the Electron window and routes to relevant scene
- [x] `electron/preload.cjs` exposes `notify` on `window.electronAPI`

**Files:**
- `electron/main.mjs` (add: notify IPC handler, notification click routing)
- `electron/preload.cjs` (add: `notify` in contextBridge)
- `app.js` (add: notification trigger points)

---

## Tier 4 — Platform

*What makes it an OS vs an app. Post-launch.*

---

### T4-1 — Background Task Scheduler

**Status:** IMPLEMENTED IN REPO

**What it is:**
The OS needs to do things without the user asking: sync emails, watch calendar events,
monitor health metrics. Requires T0-1 (services always running).

**Acceptance Criteria:**
- [x] `backend/scheduler.py` implements cron-style task queue (APScheduler or custom)
- [x] Tasks defined: email sync (every 5 min), calendar sync (every 5 min), relay heartbeat
- [x] Task results stored in SQLite, queryable by scene renderer
- [x] Tasks run in background thread — do not block request handling
- [x] Tasks can be paused/resumed via `/api/scheduler` endpoint

**Files:**
- `backend/scheduler.py` (new)
- `backend/main.py` (modify: start scheduler on boot)
- `requirements.txt` (add: APScheduler if used)

---

### T4-2 — Multi-Window / Multi-Scene

**Status:** IMPLEMENTED IN REPO

**What it is:**
Today: one Electron window, one scene at a time.
Target: multiple scenes side-by-side (like multiple app windows on a real OS).

**Acceptance Criteria:**
- [x] Electron supports opening a second `BrowserWindow` via IPC
- [x] Each window loads the same app but with its own scene state (isolated)
- [x] Keyboard shortcut to open new scene window (Cmd/Ctrl+N)
- [x] Scenes in different windows can reference the same session graph data
- [x] Closing a scene window does not affect other windows

**Files:**
- `electron/main.mjs` (add: multi-window support)
- `app.js` (add: scene isolation per window)

---

### T4-3 — Cross-Device Continuity (Handoff)

**Status:** IMPLEMENTED IN REPO, DEVICE E2E STILL PENDING

**What it is:**
Infrastructure exists (handoff tokens, relay transport, presence heartbeat).
Mobile skeleton exists. Wire it end-to-end.

**Acceptance Criteria:**
- [x] APNs device token registered on iOS app launch, stored in relay
- [x] FCM device token registered on Android, stored in relay
- [x] Handoff gesture on desktop sends scene state to relay → push wakes mobile
- [x] Mobile app receives handoff, renders equivalent scene
- [x] EAS build configured for iOS (TestFlight distribution)
- [ ] End-to-end test: desktop → relay → mobile handoff verified

**Files:**
- `Nous/nous-mobile/src/push_tokens.ts` (modify: registration flow)
- `backend/push_dispatch.py` (verify: APNs/FCM wired)
- `Nous/nous-mobile/App.tsx` (modify: handoff receive handler)

---

### T4-4 — Fine-Grained Permission Model

**Status:** IMPLEMENTED IN REPO

**What it is:**
Today: all scenes access all connectors.
Target: scene-level OAuth scope gating.
Weather scene cannot read Gmail. Music scene cannot access calendar.

**Acceptance Criteria:**
- [x] `CAPABILITY_REGISTRY` in `main.py` extended with `connector_scopes` per intent
- [x] Backend validates: before calling a connector, check the requesting intent has scope
- [x] Scene renderer receives only the data its intent is scoped to
- [x] Scope violations return 403 with clear error (not silent empty data)
- [x] User can review and revoke per-scene connector grants in OS settings

**Files:**
- `backend/main.py` (modify: CAPABILITY_REGISTRY + scope validation)
- `app.js` (add: settings scene for permission review)

---

### T4-5 - Paired Surface Registry

**Status:** IN PROGRESS

**What it is:**
Today: cross-device takeover mostly depends on session-local presence.
Target: Genome keeps a durable registry of paired screens/surfaces, so a phone can be targeted as a known Genome surface before that specific session is already active there.

**Acceptance Criteria:**
- [x] Paired surfaces persisted durably outside the volatile session-presence map
- [x] Backend exposes register/list APIs for installed Genome surfaces
- [x] Mobile/installed surface registration stores DID, relay URL, and push tokens together
- [x] Handoff start prefers a paired mobile surface when session-local presence has no viable target
- [ ] Named profiles / user-owned surface grouping beyond the default local profile

**Files:**
- `backend/db.py` (add: paired surface storage)
- `backend/main.py` (add: paired surface registry + handoff target selection)
- `Nous/nous-mobile/src/genomeui_handoff.ts` (modify: register installed surface globally)

---

### T4-6 - Relay-First Screen Takeover

**Status:** IN PROGRESS

**What it is:**
Today: QR/link fallback still appears when Genome cannot identify a target surface.
Target: installed Genome surfaces wake and resume over relay/push first, with QR only as a last-resort bootstrap path.

**Acceptance Criteria:**
- [x] Desktop handoff emits signed relay takeover envelopes
- [x] Installed mobile surface can claim the same Genome session from relay-delivered payloads
- [x] Push wake payload carries enough context to resume the same session without HTTP-first flow
- [x] Desktop shell treats relay/push takeover as the primary success path
- [ ] Physical device E2E verified: desktop requests takeover, phone resumes the same Genome session

**Files:**
- `backend/main.py` (modify: relay-first handoff dispatch)
- `backend/push_dispatch.py` (verify: wake payload contract)
- `app.js` (modify: relay-first UX)
- `Nous/nous-mobile/App.tsx` (modify: relay wake/resume path)

---

### T4-7 - Surface Targeting And Preference Control

**Status:** IN PROGRESS

**What it is:**
Today: paired surfaces can exist, but takeover targeting is still mostly implicit.
Target: Genome can show paired screens, mark one as preferred, and direct takeover to a specific target surface from the continuity shell.

**Acceptance Criteria:**
- [x] Backend stores and returns preferred-surface state
- [x] Backend exposes a way to mark a surface as preferred
- [x] Handoff start can explicitly target a chosen paired surface
- [x] Continuity surface renders paired surfaces and their preference state
- [ ] Device-level verification with multiple real paired screens

**Files:**
- `backend/main.py` (modify: preferred surface APIs + handoff target selection)
- `app.js` (modify: paired surfaces controls in continuity shell)
- `tests/unit/test_paired_surfaces.py` (add: selection and preference coverage)

---

### T4-8 - First-Contact Surface Pairing

**Status:** IN PROGRESS

**What it is:**
Today: relay-first takeover works once a screen is already known to Genome.
Target: an installed screen can pair itself to a reachable Genome backend ahead of handoff so takeover can be tested and used on purpose.

**Acceptance Criteria:**
- [x] Installed surface can persist the chosen Genome backend URL
- [x] Installed surface can register itself as a paired Genome screen before any active handoff context
- [x] Desktop continuity surface shows the paired screen after first-contact pairing
- [x] Repo includes an explicit relay-first handoff test runbook
- [ ] Physical device verification with a freshly paired phone from a cold start

**Files:**
- `Nous/nous-mobile/src/genomeui_handoff.ts` (add: pairing bootstrap helpers)
- `Nous/nous-mobile/App.tsx` (modify: pair-this-phone flow)
- `README.md` (add: handoff pairing runbook)

---

### T4-9 - Phone PWA Surface

**Status:** IN PROGRESS

**What it is:**
Today: phone handoff still has remnants of backend/app-targeted flows.
Target: the phone runs Genome as an installable PWA surface, and handoff/share opens that same Genome surface directly.

**Acceptance Criteria:**
- [x] Service worker enabled for browser/PWA runtime while staying disabled in Electron
- [x] Phone runtime exposes an install path (`beforeinstallprompt` / Add to Home Screen guidance)
- [x] Handoff QR/share path opens the Genome phone surface URL, not a raw backend endpoint
- [x] README documents the phone PWA install/test flow
- [ ] Device-level verification from a freshly installed home-screen surface

**Files:**
- `app.js` (modify: PWA runtime + phone share path)
- `public/sw.js` (use: browser shell caching)
- `public/manifest.json` (use: install metadata)
- `README.md` (update: phone PWA testing flow)

---

## v0.1 Closed Beta — Definition of Done

The following must ALL be true before inviting any external user:

- [x] **T0-1**: Nous + Backend registered as Windows logon tasks, auto-restart on crash
- [x] **T0-2**: Sessions persisted in SQLite — survive backend restart
- [ ] **T0-3**: Intent classification completes in <500ms end-to-end
- [x] **T0-4**: Boot always shows Latent Surface (no scene restore)
- [ ] **T1-1**: Nous fine-tuned as general assistant — intent accuracy >90%, general capability preserved
- [ ] **T2-1**: Gmail returns real inbox data
- [ ] **T2-2**: Google Calendar returns real events
- [x] **T2-4**: Token refresh working for all live connectors
- [x] **T3-3**: Desktop notifications fire for reminders and messages
- [x] Auth enabled by default (`GENOME_AUTH_ENABLED=true`)

---

## Execution Order

```
NOW:
  T0-1  Process supervision (Windows services)
  T0-3  Nous model speed (<200ms)
  T0-4  Clean boot to Latent Surface

SPRINT 1:
  T0-2  SQLite session persistence
  T2-4  Token refresh helper
  T2-1  Gmail live
  T2-2  Google Calendar live

SPRINT 2:
  T1-1  Fine-tune Nous (general assistant)
  T1-2  Gateway performance instrumentation
  T2-3  Spotify live
  T3-3  Desktop notifications

SPRINT 3:
  T3-1  Auto-update
  T3-2  Crash reporting
  T1-3  Agentic multi-step

POST-LAUNCH:
  T4-1  Background scheduler
  T4-2  Multi-window
  T4-3  Cross-device handoff
  T4-4  Permission model
```
