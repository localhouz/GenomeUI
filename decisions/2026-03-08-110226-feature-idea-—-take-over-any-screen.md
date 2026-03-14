---
tags: ["feature", "screen-cast", "handoff", "continuity", "wild-want"]
category: decisions
created: 2026-03-08T11:02:26.699839
---

# Feature Idea — Take Over Any Screen

# Feature Idea — Take Over Any Screen

## Concept
GenomeUI should be able to project/render its interface onto any nearby screen — TV, monitor, tablet, secondary display — on demand. "Take over any screen" means the current scene (or any scene) renders on a target device without that device needing GenomeUI installed.

## Why it fits the OS philosophy
- Screens are resources, not endpoints. The OS should control all of them.
- Continuity/handoff is already in the taxonomy (Wave-4: handoff.push, handoff.pull, handoff.continue)
- The rendering model is already generative + canvas-based — it's device-agnostic by design
- Directly competes with Apple AirPlay / Google Cast but as an OS primitive, not a media feature

## Technical approaches (ranked by feasibility)

### 1. WebRTC screen push (most feasible)
- GenomeUI frontend opens a WebRTC peer connection
- Target screen visits a simple URL (e.g. `genome.local/cast`) — no app needed
- Backend acts as signaling server
- Scene canvas + HTML layer streamed as video track
- Works on any browser-capable screen (TV, tablet, phone, monitor with browser)

### 2. Chromecast / Google Cast API
- Frontend uses Cast SDK to push a URL to Chromecast-enabled TVs
- Receiver: a minimal GenomeUI cast receiver page hosted on backend
- Requires Cast SDK (available in Chrome)

### 3. DLNA / UPnP (TV-native)
- Backend discovers DLNA renderers on LAN
- Pushes a stream URL
- Most smart TVs support this natively
- Limited to media content (not interactive scenes)

### 4. Second browser window (simplest, local only)
- Open a second window in "cast mode" on another display
- No networking required, works immediately
- Limited to same-machine multi-monitor

## Implementation sketch

### Backend additions
- `GET /api/cast/sessions` — list active cast targets
- `POST /api/cast/push` — push current scene to target
- `GET /cast` — receiver page (minimal HTML that renders a GenomeUI scene from session state)
- WebSocket: target subscribes to session updates and re-renders on each turn

### Frontend additions
- Cast intent: "cast to living room TV", "show this on the big screen"
- Taxonomy op: `screen.cast` (already in Wave-4: `screen_capture` domain exists, could extend)
- Scene dock: small cast indicator when a screen is being driven
- Discovery: mDNS/Bonjour scan for devices running `/cast` receiver

### Intent examples
- "put this on the TV" → screen.cast → discover LAN targets → push scene
- "take over the conference room screen" → screen.cast with room name slot
- "stop casting" → screen.cast_stop

## Connection to existing taxonomy
- `handoff.push` / `handoff.pull` — already in Wave-4
- `screen_capture` domain — exists, could be extended
- New op needed: `screen.cast`, `screen.discover`, `screen.cast_stop`

## Priority
Medium-high — high visual impact, differentiates strongly from standard OSs, technically achievable with WebRTC approach in a single session.

