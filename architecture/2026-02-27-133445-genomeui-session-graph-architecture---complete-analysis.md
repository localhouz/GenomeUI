---
tags: ["session-graph", "ipc-protocol", "nous-integration", "data-flow"]
category: architecture
created: 2026-02-27T13:34:45.428828
---

# GenomeUI Session Graph Architecture - Complete Analysis

# GenomeUI Session Graph Architecture - Complete Analysis

**Date:** 2026-02-27
**Purpose:** Foundation for Nous IPC protocol design

## 1. SESSION GRAPH STRUCTURE

### 1.1 Graph Data Model
Location: `backend/main.py:340-364` (SessionState class)

The graph is part of SessionState and has this structure:
```python
graph: dict[str, Any] = {
    "entities": [...],      # Array of all nodes
    "relations": [...],     # Array of edges between nodes
    "events": [...]         # Audit log of graph mutations
}
```

### 1.2 Entity Schema
Created via `graph_add_entity(graph, payload)` at line 18292

Each entity has:
```javascript
{
    "id": "<uuid>",                    // Unique identifier (auto-generated)
    "kind": "task" | "expense" | "note",  // Entity type (normalized)
    
    // For TASK entities:
    "title": "string",                 // Task description
    "done": boolean,                   // Completion status
    
    // For EXPENSE entities:
    "amount": float,                   // Dollar amount
    "category": "string",              // Expense category (e.g., "food", "cloud")
    "note": "string",                  // Optional description
    
    // For NOTE entities:
    "text": "string",                  // Note content
    
    // Common fields on all:
    "createdAt": number                // Millisecond timestamp
}
```

### 1.3 Relation Schema
Created via `graph_add_relation(graph, source_id, target_id, relation)` at line 24805

```javascript
{
    "sourceId": "<uuid>",
    "targetId": "<uuid>",
    "relation": "depends_on" | "references",
    // Additional fields as stored
}
```

Valid relations:
- `depends_on`: task → task only (acyclic)
- `references`: any → any

### 1.4 Event Schema
Created via `graph_add_event(graph, kind, payload)` at line 18305

```javascript
{
    "id": "<8-char-uuid>",
    "kind": "add_task" | "add_expense" | "add_note" | "toggle_task" | "delete_task" | "link_entities" | ... ,
    "payload": { /* operation-specific data */ },
    "createdAt": number                // Millisecond timestamp
}
```

Events are **capped at 2000** per graph (line 18309). This is an audit trail showing all mutations.

---

## 2. DATA FLOW: FRONTEND → BACKEND → GRAPH → FRONTEND

### 2.1 Frontend → Backend (Intent Submission)

**Location:** `app.js:54-79` (RemoteTurnService.process)

Frontend sends to `POST /api/turn`:
```javascript
{
    intent: "string",              // User text input
    sessionId: "string",           // Current session ID
    baseRevision: number,          // Local revision for conflict detection
    deviceId: "string",            // Client device identifier
    onConflict: "rebase_if_commutative",  // Merge policy
    idempotencyKey?: "string"      // Optional dedup key
}
```

### 2.2 Backend Intent Envelope (backend/main.py:12854)

Backend receives intent and compiles it into an **Intent Envelope** via `compile_intent_envelope(text)`:

```javascript
{
    "surfaceIntent": {
        "raw": "original user text",
        "normalized": "lowercase normalized",
        "kind": "question" | "command" | "statement",
        "timestamp": number
    },
    
    "taskIntent": {
        "goal": "mutate" | "overview" | "inspect",
        "operation": "write" | "read",
        "targetDomains": ["tasks", "expenses", "notes", ...],
        "constraints": []
    },
    
    "stateIntent": {
        "readDomains": ["tasks", "expenses", ...],
        "writeOperations": [
            {
                "type": "add_task" | "toggle_task" | "add_expense" | ...,
                "domain": "tasks" | "expenses" | "notes",
                "payload": { /* operation-specific payload */ }
            }
        ],
        "summary": "add_task + add_expense"  // Concise operation list
    },
    
    "uiIntent": {
        "mode": "default",
        "density": "normal",
        "interactionPattern": "prompt + suggestions",
        "emphasis": "balanced"
    },
    
    "confidence": 0.42 to 0.96,       // Confidence score
    "intentClass": "string",          // Semantic intent classification
    
    "clarification": {
        "required": boolean,
        "question": "string (if required)",
        "examples": ["suggestion1", "suggestion2"]
    },
    
    "confidencePolicy": {
        "threshold": 0.65,
        "needsClarification": boolean
    }
}
```

### 2.3 Backend Graph Mutation (execute_operations, backend/main.py:17318)

The `stateIntent.writeOperations` array is executed against the graph.

Each operation calls `run_operation(session, op)` which mutates `session.graph`.

### 2.4 Session State Update (backend/main.py:7775-7785)

After execution, the backend stores in `session.last_turn`:
```javascript
{
    "intent": "original user text",
    "envelope": { /* the compiled intent envelope */ },
    "execution": { /* the result from execute_operations */ },
    "kernelTrace": { /* performance/routing data */ },
    "plan": { /* UI rendering plan */ },
    "planner": "string",
    "route": { /* routing info */ },
    "merge": { /* merge metadata */ },
    "timestamp": number
}
```

Then increments `session.revision`.

### 2.5 Backend → Frontend Sync

**WebSocket:** `/ws?sessionId=<id>` - real-time push
**SSE:** `/api/stream?sessionId=<id>` - fallback push
**HTTP Poll:** `GET /api/session/<id>` - every 2.5s as last resort

All three return via `session_sync_payload()` at line 7956:
```javascript
{
    "type": "session_sync",
    "sessionId": "string",
    "revision": number,
    "memory": { "tasks": [...], "expenses": [...], "notes": [...] },
    "presence": { ... },
    "handoff": { ... },
    "continuityAutopilot": { ... },
    "lastTurn": {
        "intent": "string",
        "envelope": { /* FULL INTENT ENVELOPE */ },
        "execution": { ... },
        "kernelTrace": { ... },
        "plan": { ... },
        ...
    }
}
```

### 2.6 Frontend Graph Snapshot (app.js:1572-1598)

Frontend fetches `GET /api/session/<id>/graph?limit=300`:
```javascript
{
    "ok": true,
    "sessionId": "string",
    "counts": { "entities": number, "relations": number, "events": number, ... },
    "entities": [ /* last 300 entities */ ],
    "relations": [ /* last 300 relations */ ],
    "events": [ /* last 300 events */ ]
}
```

Stored in `this.state.session.graphSnapshot`.

---

## 3. AVAILABLE FIELDS FOR NOUS INTEGRATION

### 3.1 Intent Envelope Fields (Already Present)

**In `envelope.stateIntent`:**
- `readDomains` - what domains were queried
- `writeOperations` - array of operations parsed from text
- `summary` - concise operation summary

**In `envelope.taskIntent`:**
- `goal` - "mutate", "overview", or "inspect"
- `operation` - "write" or "read"
- `targetDomains` - domains involved

**In `envelope.surfaceIntent`:**
- `kind` - "question", "command", or "statement"
- `normalized` - lowercased text

**In `envelope`:**
- `confidence` - confidence score (0.42 to 0.96)
- `intentClass` - semantic classification (can be extended)
- `clarification` - if clarification is needed

### 3.2 Graph Events (Already Present)

Each `graph.events[i]` contains:
- `kind` - the operation type that fired
- `payload` - operation-specific data
- `createdAt` - when it happened

### 3.3 Proposed Field for Nous Classification

Add to envelope before execution:
```javascript
"envelope": {
    // ... existing fields ...
    
    "nousIntent": {
        "classified": boolean,
        "domain": "string",              // Nous's domain classification
        "intent": "string",              // Nous's intent classification
        "confidence": number,
        "source": "nous",
        "classifiedAt": number
    }
}
```

This flows through to frontend via `lastTurn.envelope` automatically.

---

## 4. WATCHING/POLLING MECHANISMS

### 4.1 Backend → Frontend Push (Already Exists)

**WebSocket** (preferred):
- Endpoint: `/ws?sessionId=<id>`
- Handler: `openWebSocketSync()` in app.js:574
- Triggers on every `broadcast_session()` call

**SSE** (fallback):
- Endpoint: `/api/stream?sessionId=<id>`
- Same payload as WebSocket

### 4.2 Frontend → Backend Poll (Last Resort)

**HTTP Polling** (app.js:664-685):
- Endpoint: `GET /api/session/<id>`
- Frequency: Every 2.5s (FALLBACK_POLL_MS)
- Only if WebSocket/SSE fails
- Updates via `applyRemoteSync()` or `pollSession()`

### 4.3 Graph-Specific Polling

Frontend fetches graph on-demand:
- Endpoint: `GET /api/session/<id>/graph?limit=300`
- Trigger: When `graphSnapshotRevision < current revision`
- Cached in `this.state.session.graphSnapshot`

### 4.4 Nous Integration Points

Nous needs to:

1. **Read current session graph:**
   ```
   GET /api/session/{sid}/graph
   ```

2. **Read session data:**
   ```
   GET /api/session/{sid}
   ```
   Includes full `lastTurn.envelope`

3. **Publish classification** - options:
   - A: HTTP POST to classification endpoint
   - B: Write events directly via operation
   - C: Store in separate intent table

4. **Watch for changes:**
   - Poll `/api/session/{sid}` for new turns
   - Subscribe to WebSocket at `/ws?sessionId={sid}`
   - Parse `lastTurn.envelope.stateIntent.writeOperations`
   - Return classification

---

## 5. SESSION PERSISTENCE

**Storage:** `backend/data/sessions.json` (line 42)

**Serialization** (line 2773):
The entire `session.graph` and `session.last_turn` (including full envelope) are persisted.

This means:
- Nous can access historical intent envelopes via session restoration
- All graph states are recoverable
- All intent classifications become part of the audit trail

---

## 6. SUMMARY: NOUS IPC PROTOCOL DESIGN

### Key Architectural Points:

1. **Session Graph is Source of Truth**
   - Fully structured (entities, relations, events)
   - Already persisted and synced
   - Nous can read via `/api/session/{sid}/graph`

2. **Intent Envelope is Classification Input**
   - `stateIntent` contains parsed operations
   - `surfaceIntent` contains original text  
   - `taskIntent` contains high-level goal
   - Nous should enhance via new `nousIntent` field

3. **Three Sync Mechanisms Available**
   - WebSocket (real-time) - ideal for Nous updates
   - SSE (fallback)
   - HTTP polling (reliable)

4. **No Extra Storage Needed**
   - Session already has `last_turn` with full envelope
   - Session already has `graph.events` for audit
   - Nous classification can embed in `envelope.nousIntent`

5. **Revision-Based Idempotency**
   - Each turn increments `session.revision`
   - Nous can use same pattern for classification idempotency

### Proposed Nous IPC Flow:

```
1. Frontend submits intent to /api/turn
2. Backend compiles envelope (existing)
3. Backend optionally calls Nous (NEW)
   POST /nous/classify
   {
       "sessionId": "...",
       "text": "user input",
       "graphContext": { entities, relations },
       "currentIntentClass": "from classify_intent()"
   }
4. Nous returns
   {
       "domain": "...",
       "intent": "...",
       "confidence": number
   }
5. Backend enriches envelope.nousIntent (NEW)
6. Backend executes operations
7. Backend broadcasts via WebSocket with enriched envelope
8. Frontend renders with Nous classification
```

**OR** Nous watches via WebSocket at `/ws?sessionId={sid}` and publishes classifications asynchronously.

---

## File References

- **Session state:** `backend/main.py:340-364`
- **Graph operations:** `backend/main.py:18281-18810`
- **Intent envelope:** `backend/main.py:12854-12900`
- **Turn endpoint:** `backend/main.py:7611-7812`
- **Sync payload:** `backend/main.py:7956-7966`
- **Frontend sync:** `app.js:574-685`
- **Frontend graph fetch:** `app.js:1572-1598`
- **Serialization:** `backend/main.py:2773-2827`
