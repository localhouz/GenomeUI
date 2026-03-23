Plan: GenomeUI — Next Build Sprint
Part 1: Generative UI Gap Analysis
Context
Research into best-in-class 2026 generative UIs (AG-UI/CopilotKit, A2UI/Google Dec 2025,
Vercel AI SDK) surfaced 5 high-impact gaps in GenomeUI that should be built after the
functional surfaces below are complete.

What to Build (ordered by impact)
1. Tool Lifecycle UI — Biggest gap
AG-UI's core pattern: tool_call_start → tool_call_streaming → tool_call_end | error.
GenomeUI today: blank canvas for 1–3s, then scene snaps in fully-formed.
What to build: turn-state machine (idle → thinking → streaming → done → error).
On thinking: render scene skeleton (chrome at 20% opacity + shimmer). EventSource
events progressively fill it. done: scene snaps to full render.
Files: app.js (turn state machine + skeleton renderers), index.css (shimmer keyframe)

2. Human-in-the-Loop Confirmation UI
CAPABILITY_REGISTRY already has risk: "high" entries — nothing reads them at runtime.
What to build: _confirmHighRisk(op, body) gate in run_operation() returns
{ ok: false, needsConfirm: true, op, summary } for high-risk ops. Frontend renders a
kind: 'confirm' surface. Approve re-submits with {"confirmed": true} in body.
Files: backend/main.py (risk gate), app.js (confirm scene), index.css (confirm styles)

3. History Reel with Visual Thumbnails
this.state.history (40 turns) exists. Reel renders text chips only — no scene previews.
What to build: on scene render, capture canvas.toDataURL('image/jpeg', 0.4). Store
alongside history entry. Render reel items as 80×52px thumbnails + intent label.
Hover expands to 200×130px. Click re-renders that turn.
Files: app.js (thumbnail capture + reel renderer update)

4. Semantic Cache (Instant Replay)
Every intent makes a full POST round-trip even for identical queries.
What to build: SemanticCache keyed by sha1(intent.trim().toLowerCase()).
TTL per domain: weather 10min, sports 2min, banking 5min, shopping 30min, tasks 0.
Check cache before POST; if hit render immediately, silently refresh in background.
Files: app.js (SemanticCache class + integration into RemoteTurnService.process)

5. Sound Engine
Zero audio feedback. All feedback is visual.
What to build: SoundEngine singleton using Web Audio API (synthesized, no files).
Tones: scene transition (soft ascending chord), error (descending minor 2nd),
success (ping), button click (4kHz transient). Default off, user toggles.
Files: app.js (SoundEngine class + integration points)

What We Are NOT Missing
Scene variety (29 kinds), canvas animation depth (21 renderers), security model, content model (git-style revision graph), domain coverage (355 intents) — all ahead of or equal to best-in-class.
Part 2: Two-Surface Architecture + Real Computer Scenes
Context
GenomeUI needs to be adoptable as a daily computing replacement — not just a novelty. That
requires a clear design principle: not everything should be experiential. Information
scenes (weather, banking, music, email inbox) stay full-bleed and ambient. Work scenes
(documents, spreadsheets, code, presentations, terminal) render as real tools — familiar,
functional, traditional. The canvas theatrics don't belong in a budget spreadsheet.

Email stays experiential (inbox/reading/compose). Calendar stays experiential for viewing;
edit interactions happen via intent or inline form overlays — not a functional surface.

AI is embedded in functional surfaces: the intent bar still works ("add a totals row",
"bold the header", "refactor this function"). The tool feels traditional; the agent
makes it smarter.

Design Principle: Two Surface Modes
Mode	When	Look	Canvas
experiential	All current scenes (weather, banking, music, shopping, contacts, email, health, social, …)	Full-bleed, hero text, fading streams, ambient canvas	Active — weather, particles, spotlights, etc.
functional	document, spreadsheet, presentation, code, terminal	App-like — white/near-white content area, toolbar, standard controls	Hidden or minimal neutral bg only
Routing: buildCoreSurface() sets mode: 'functional' when kind is one of
document | spreadsheet | presentation | code | terminal. Everything else stays 'experiential'.

Content Store (Foundational — Build First)
All functional surfaces need a persistence layer. This is now urgent, not theoretical.

Schema (SQLite, content_store.db in backend):


CREATE TABLE content_objects (
  hash       TEXT PRIMARY KEY,         -- sha256 of content
  delta_from TEXT REFERENCES content_objects(hash), -- NULL = full snapshot
  delta_data BLOB,                     -- full content or JSON patch
  created_at REAL                      -- unix timestamp
);

CREATE TABLE content_heads (
  name       TEXT,                     -- "Q4 Report", "Budget 2026"
  domain     TEXT,                     -- 'document' | 'spreadsheet' | 'code' | etc.
  hash       TEXT REFERENCES content_objects(hash),  -- current version
  updated_at REAL,
  PRIMARY KEY (name, domain)
);
Commit granularity: Intent-level, not keystroke-level. User edits freely; delta
committed on intent submit or 30s inactivity blur. Never per-keystroke.

In-memory exposure: Current version only (a few KB). Old versions on disk,
lazy-loaded if history reel or undo requests them.

Backend endpoints to add:

GET /api/content/{domain}/{name} — load current version
POST /api/content/{domain}/{name} — commit new delta
GET /api/content/{domain} — list all content heads for a domain
DELETE /api/content/{domain}/{name} — soft-delete (tombstone)
File: backend/main.py (new content store routes + _content_db helper)

Functional Surface: Document
Looks like: A word processor. White page, top toolbar, body text area.

Toolbar (minimal): B / I / U / H1 / H2 / — (HR) / • (list) / 1. (ordered) / [ ] (table)

Implementation: contenteditable div with execCommand for formatting (or a thin
custom approach — no heavy library needed for this scope). No ProseMirror, no Tiptap.

Storage format: HTML (serialize inner HTML on commit; render by setting innerHTML on load)

AI integration: On intent submit while document is active, backend receives current
HTML content in body → agent modifies it → returns updated HTML → surface re-renders.
Cursor position preserved via Range save/restore.

Scene HTML structure:


<div class="scene scene-document functional-surface">
  <div class="func-toolbar">
    <button data-cmd="bold">B</button>
    <button data-cmd="italic">I</button>
    <!-- ... -->
    <span class="func-doc-name">Q4 Report</span>
    <span class="func-save-status">Saved</span>
  </div>
  <div class="func-doc-body" contenteditable="true">...</div>
</div>
Functional Surface: Spreadsheet
Looks like: A spreadsheet. Column headers (A–Z), row numbers, formula bar, editable cells.

Implementation: Hand-built grid — <table> with contenteditable TDs. Formula bar
above (shows cell value/formula for selected cell). No library needed at minimal scope.

Formula support (Phase 1): =SUM(A1:A10), =AVG(B2:B8), =IF(C1>0,"Y","N"),
=A1+B1, cell references. Parse on blur/enter, evaluate immediately.

Storage format: JSON — { cells: { "A1": { value: "Revenue", formula: null }, "B1": { value: 42000, formula: "=SUM(C1:C10)" } }, colWidths: {...}, rowHeights: {...} }

AI integration: Same pattern — current cells JSON sent with intent → agent returns
updated cells JSON → grid re-renders.

Scene HTML structure:


<div class="scene scene-spreadsheet functional-surface">
  <div class="func-toolbar">
    <input class="func-formula-bar" placeholder="Formula bar…" />
    <span class="func-doc-name">Budget 2026</span>
  </div>
  <div class="func-sheet-wrap">
    <table class="func-sheet-grid">...</table>
  </div>
</div>
Functional Surface: Code
Looks like: A code editor. Line numbers, monospace, syntax highlighting.

Implementation: Embed CodeMirror 6 (modular, no build step, import from CDN via
ES module). Import: @codemirror/view, @codemirror/state, language pack for detected
language. ~80KB gzipped total — acceptable.

Language detection: From intent ("write a Python script…" → python; file extension
in name → auto-detect). Falls back to javascript.

Storage format: Plain text + { language, name } metadata.

AI integration: Current code text sent with intent → agent returns updated code →
CodeMirror setState with new doc.

Functional Surface: Presentation
Looks like: A slide deck editor. Slide panel left (thumbnails), editing area right.

Implementation: Slides are divs. Active slide is a contenteditable div with a
fixed aspect ratio (16:9). Slide panel shows miniature previews. Add/delete/reorder
slides via toolbar buttons or intent.

Storage format: JSON array of slides — [{ id, layout, bg, elements: [{type, text, x, y, w, h, style}] }]

AI integration: Current slides JSON + intent → agent returns updated slides JSON.

Functional Surface: Terminal
Looks like: A terminal. Black bg, monospace, scrollback buffer, prompt.

Implementation: Input line at bottom, output above. History via arrow keys. For now:
commands are dispatched to the backend's terminal_exec op (sandboxed). Later: real
shell via WebSocket.

Storage format: Not persisted — session-scoped scrollback only.

CSS for Functional Surfaces
Functional surfaces override the scene's experiential chrome:


/* Shared functional surface shell */
.functional-surface {
  background: #f9f9f7;           /* near-white, not pure white */
  color: #1a1a1a;
  display: flex;
  flex-direction: column;
  position: absolute;
  inset: 0;
}

.func-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  background: #f0efe9;
  border-bottom: 1px solid rgba(0,0,0,0.08);
  flex-shrink: 0;
}

.func-toolbar button {
  font-size: 12px;
  padding: 3px 8px;
  border: 1px solid rgba(0,0,0,0.15);
  border-radius: 3px;
  background: white;
  cursor: pointer;
}

.func-toolbar button:hover { background: #e8e8e4; }

.func-doc-name {
  margin-left: auto;
  font-size: 12px;
  color: rgba(0,0,0,0.45);
  font-style: italic;
}

.func-save-status {
  font-size: 11px;
  color: rgba(0,0,0,0.3);
  margin-left: 8px;
}

/* Document body */
.func-doc-body {
  flex: 1;
  overflow-y: auto;
  padding: 48px 72px;
  font-size: 15px;
  line-height: 1.7;
  max-width: 760px;
  margin: 0 auto;
  outline: none;
  width: 100%;
}

/* Spreadsheet */
.func-sheet-wrap {
  flex: 1;
  overflow: auto;
}

.func-formula-bar {
  flex: 1;
  font-family: monospace;
  font-size: 13px;
  border: 1px solid rgba(0,0,0,0.15);
  border-radius: 3px;
  padding: 2px 8px;
}

.func-sheet-grid {
  border-collapse: collapse;
  font-size: 13px;
}

.func-sheet-grid th {
  background: #f0efe9;
  border: 1px solid #d0cec8;
  padding: 2px 8px;
  min-width: 80px;
  text-align: center;
  font-weight: 500;
  color: rgba(0,0,0,0.5);
  user-select: none;
}

.func-sheet-grid td {
  border: 1px solid #e0deda;
  padding: 1px 6px;
  min-width: 80px;
  height: 22px;
  outline: none;
}

.func-sheet-grid td:focus {
  outline: 2px solid #4285f4;
  outline-offset: -1px;
  background: #fff;
}

/* Terminal */
.func-terminal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-family: monospace;
  font-size: 13px;
  background: #0d0d0d;
  color: #e0e0e0;
}

.func-terminal-input-row {
  display: flex;
  align-items: center;
  padding: 8px 16px;
  background: #0d0d0d;
  border-top: 1px solid rgba(255,255,255,0.07);
}

.func-terminal-prompt {
  color: #4af626;
  margin-right: 8px;
  font-family: monospace;
  font-size: 13px;
}

.func-terminal-input {
  flex: 1;
  background: none;
  border: none;
  outline: none;
  color: #e0e0e0;
  font-family: monospace;
  font-size: 13px;
}
Right Rail in Functional Mode
The right rail stays but its content changes:

Document: word count, character count, last saved, intent suggestions ("summarize", "make formal", "add a conclusion")
Spreadsheet: selected cell ref, cell count, sum of selection, intent suggestions
Code: language, line count, intent suggestions ("add error handling", "write tests for this")
Terminal: last exit code, current directory (if known)
Canvas is hidden (display: none) in functional mode — no animation, neutral page bg only.

Files to Modify
File	Changes
backend/main.py	Content store DB init, CRUD endpoints, functional_doc_update op handler
app.js	buildCoreSurface mode routing; functional surface renderers (doc, sheet, code, terminal, presentation); toolbar event handlers; formula evaluator; CodeMirror init; auto-save debounce; intent-driven edit (send current content with turn body)
index.css	Functional surface CSS (above); canvas hidden in functional mode
Combined Build Order
Phase A — Functional Surfaces (Part 2 first — biggest adoption unlock)

Content store — SQLite schema + CRUD endpoints (backend/main.py)
Functional surface CSS — base styles, toolbar, doc body, sheet grid, terminal (index.css)
Document editor — contenteditable + toolbar + save/load + AI edit loop (app.js)
Spreadsheet — grid + formula bar + formula evaluator + save/load + AI edit loop (app.js)
Code editor — CodeMirror 6 import + language detection + save/load + AI edit loop (app.js)
Terminal — scrollback + prompt + command dispatch to backend (app.js)
Presentation — slide panel + editing surface + save/load + AI edit loop (app.js)
Phase B — Generative UI Improvements (Part 1)
8. Tool lifecycle UI + skeletons — turn state machine, shimmer animation
9. Human-in-the-loop confirm — risk gate in backend, confirm scene in frontend
10. History reel thumbnails — canvas capture + reel renderer
11. Semantic cache — SemanticCache class + RemoteTurnService integration
12. Sound engine — Web Audio API synthesis + toggle

Verification
Say "create a document called Q4 Report" → functional document surface renders (white page, toolbar) with agent-generated scaffold content
Click into document body → cursor appears, can type normally
Select text, click B in toolbar → bolds
Say "make the intro paragraph more formal" → backend receives current HTML + intent → returns updated HTML → document updates in place, cursor restored
Say "create a spreadsheet called Budget 2026" → functional spreadsheet renders (grid, formula bar)
Click a cell → can type value; type =SUM(A1:A3) in formula bar → evaluates
Say "add a row for Q3 expenses" → agent updates cells JSON → grid re-renders
Say "open a code editor" → CodeMirror surface renders with syntax highlighting
Say "write a Python function to calculate compound interest" → agent returns code → editor populates
All saves persist to SQLite content store; reload → content survives
User approved the plan
Update Todos

Content store — SQLite schema + CRUD endpoints in backend/main.py

Functional surface CSS — base styles, toolbar, doc/sheet/terminal in index.css

Document editor — contenteditable + toolbar + save/load + AI edit loop in app.js

Spreadsheet — grid + formula bar + formula evaluator + save/load + AI edit loop in app.js

Code editor — CodeMirror 6 + language detection + save/load + AI edit loop in app.js

Terminal — scrollback + prompt + command dispatch in app.js

Presentation — slide panel + editing surface + save/load + AI edit loop in app.js
Starting with the content store. Let me read the relevant sections of main.py.

Read c:\Users\steve\Documents\GenomeUI\backend\main.py
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 101-180)
Let me find the startup event and auth DB initialization to understand where to add the content store init.

Grep "startup|_init_db|sqlite|auth_vault|on_event" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
41 lines of output
Grep "connector_vault|vault_store|vault_retrieve" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
21 lines of output
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 3490-3549)
Now let me find the connectors endpoints section and the end of run_operation to understand where to add handlers:

Grep "^@app\.(get|post|delete)\("/api/connectors" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
21 lines of output
Grep "return \{"ok": False, "message": "Unknown operation" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
1 line of output
Grep "Unknown op|unknown op|Unknown operation|unrecognised|unrecognized" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
3 lines of output
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 31856-31895)
Grep "document_create|document_edit|spreadsheet_create|code_create|presentation_create" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
9 lines of output
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 4080-4109)
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 30836-30905)
Grep "def _require_auth" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
1 line of output
Grep "require_auth\|_require_auth\|auth_check" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
2 lines of output
Grep "X-Genome-Auth|genome_session|auth_token|_auth\." (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
21 lines of output
Grep "x-genome-auth|X-Genome-Auth|session_validate|session_check" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
21 lines of output
Grep "headers.get.*auth\|request.headers.*X-Genome" (in c:\Users\steve\Documents\GenomeUI\backend\main.py)
2 lines of output
Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 3793-3822)
Now I have everything I need. Making all the content store changes:

Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Added 1 line
Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Added 1 line
Now find where to add the content store functions — after the connector vault sync functions:

Read c:\Users\steve\Documents\GenomeUI\backend\main.py (lines 1054-1073)
Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Added 83 lines
Now add the startup init call and the REST endpoints. First the startup:

Edit c:\Users\steve\Documents\GenomeUI\backend\main.py
Added 1 line
Now add the REST endpoints after the mock slack endpoint: