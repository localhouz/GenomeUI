---
tags: ["session-index", "genomeui", "mesh", "mobile", "architecture"]
category: decisions
created: 2026-03-08T11:50:52.401101
---

# GenomeUI Session Index — Mar 8 2026 (afternoon)

# GenomeUI Session Index — Mar 8 2026 (afternoon)

## What was decided this session

### Phase B — Complete (already built in prior session)
- Tool lifecycle shimmer (`.refracting` CSS class) — live
- High-risk confirm gate (`needsConfirm` backend + `_showConfirm()` frontend) — live
- Semantic cache (`SemanticCache` with per-domain TTLs) — live
- Sound engine (`SoundEngine` Web Audio API) — live
- History reel — live but hidden by default (user decision)

### History Reel — Hidden by default
- Changed `#history-reel` CSS from `display: flex` to `display: none`
- Added `.reel-visible` class to show it
- Added `showHistoryReel()` / `hideHistoryReel()` methods
- Local intent intercept: "show history" / "hide history"
- Click-outside to dismiss
- Files: `index.css`, `app.js`

## Architecture Decisions

### Deployment Model
- **No app stores** — browser/PWA delivery only
- **Two modes**: Native (full OS replacement) and Overlay (runs on top of existing OS)
- **Desktop**: Electron owns the screen — fully viable today
- **Android**: Launcher replacement (sideload APK) — fully viable today
- **iOS**: PWA best effort now, iterate — EU Digital Markets Act creates opening
- **TV**: Parked for now — requires hardware (not desired) or smart TV browser

### "Take Over Any Screen" — Gesture-based session transfer
- Phone to desktop: swipe gesture → session moves over Nous mesh
- TV: parked until viable no-friction path exists
- Gesture UX: swipe toward target screen direction → session transfers to nearest mesh node

### Shared Memory = No Handoffs
- Session memory lives on the mesh, not per-device
- All devices are mesh nodes — join mesh → already in sync
- Handoff system becomes obsolete once mesh is live

### Nous Mesh Integration Plan (next build)
Integration order:
1. Wire Nous mesh into GenomeUI backend (mesh first)
2. Build GenomeUI phone UI (clone of desktop, connects to same mesh session)
3. "Take over any screen" falls out naturally

## Nous Mesh — Current State (C:\Users\steve\Documents\Nous)
- **Feature complete** — libp2p, gossipsub, TCP transport, peer discovery
- **Protocol**: `/dcipm/mesh/1.0.0`, pubsub topic: `dcipm-mesh`
- **Key files**:
  - `src/network/libp2p_mesh.ts` — P2P mesh (libp2p + gossipsub)
  - `src/network/libp2p_network.ts` — NetworkAdapter wrapper
  - `src/network/in_memory_network.ts` — in-process adapter (for testing)
  - `src/network/network_types.ts` — NetworkMessage, NetworkPeer types
  - `src/agents/local_agent.ts` — semantic ingest/query agent
  - `nous-mobile/` — Expo React Native app (already exists)

## Mesh ↔ GenomeUI Integration Wiring Points

### Session State Schema (SessionState in main.py ~line 528)
Key fields to sync over mesh:
- `memory` — flat namespace content store
- `graph` — knowledge graph (entities/relations/events)
- `revision` — monotonic version counter
- `last_turn` — last rendered scene
- `presence` — device presence tracking
- `handoff` — cross-device handoff state (to be retired)
- `journal` — operation log (git commit log equivalent)

### Broadcast Infrastructure (already exists)
- `broadcast_session(session, payload)` at main.py ~line 9342
- `session_sync_payload()` at main.py ~line 9483
- Called after every turn, every operation
- Currently broadcasts to: WebSocket clients + asyncio queue subscribers

### Integration Plan
1. Add Nous mesh node to GenomeUI backend (Python — need JS bridge or Python libp2p)
2. Wire `broadcast_session()` → also publish to mesh
3. Subscribe to mesh → apply incoming session sync from other devices
4. Replace handoff state with mesh-native presence
5. Upgrade `revision: int` to vector clock for CRDT merge

### Key Question
Nous mesh is TypeScript/Rust. GenomeUI backend is Python.
Bridge options:
- Run Nous mesh as a sidecar process, communicate via local socket/HTTP
- Use Python libp2p (`py-libp2p`) directly in backend
- Embed mesh in the Electron/browser layer, not the backend

## Next Session Priorities
1. Decide mesh bridge architecture (sidecar vs py-libp2p vs browser-side)
2. Implement mesh integration into GenomeUI backend
3. Begin phone UI (GenomeUI clone in Expo / nous-mobile)

