---
tags: ["session-index", "genomeui", "mesh", "network", "architecture", "in-progress"]
category: decisions
created: 2026-03-09T09:22:39.420858
---

# GenomeUI Session Index Mar 9 2026 Network Architecture Build

# GenomeUI Session Index — Mar 9 2026 (Network Architecture Build)

## What was built this session

### Mesh Integration (complete)
- `Nous/src/scripts/mesh_bridge.ts` — libp2p mesh node, local TCP control plane (NDJSON)
- `GenomeUI/backend/mesh_bridge.py` — Python async client for mesh bridge
- `GenomeUI/backend/main.py` — mesh wired into startup + broadcast_session()
- `_apply_mesh_sync()` in main.py — applies incoming mesh state, last-write-wins on revision

### mDNS Peer Discovery (complete)
- `@libp2p/mdns` added to Nous package.json
- Wired into `libp2p_mesh.ts` createLibp2p config as `mdns: mdns()`
- Devices on same LAN auto-discover each other, zero config

### Persistent Identity (complete)
- `GenomeUI/backend/identity.py` — ed25519 keypair, OS keychain storage, DID, BIP39 recovery
- `Nous/src/network/libp2p_mesh.ts` — `privateKeySeedHex` config option → stable peer ID
- `Nous/src/scripts/mesh_bridge.ts` — accepts seed hex as CLI arg
- `GenomeUI/backend/mesh_bridge.py` — passes seed_hex when spawning bridge
- `GenomeUI/backend/main.py` — loads identity at startup, logs DID, passes seed to mesh
- `mnemonic` Python package installed

### Network Type Architecture (IN PROGRESS — interrupted)
- `Nous/src/network/genome_envelope.ts` — CREATED, compile pending
  - `NetworkType` enum: personal | p2p | private_group | public_local | public_topic | public_global
  - `GenomeEnvelope` interface with: v, networkType, networkId, from (DID), payload, ts, sig
  - `canonicalBytes()` — deterministic signing bytes
  - `publicKeyBytesFromDid()` — extract ed25519 pubkey from did:key DID
  - `seal()` — sign envelope with private key
  - `verify()` — timestamp check + signature verification (uses @libp2p/crypto publicKeyFromRaw)
  - MAX_AGE_MS = 5min replay protection

## What's still to build (network architecture)

### Remaining tasks in order:
1. **network_registry.ts** (Nous) — NetworkRegistry class, join/leave, topic mapping
   - Topic patterns:
     - personal: `genome-session-sync` (already exists)
     - p2p: `genome-p2p-<did-hash>`
     - private_group: `genome-group-<groupId>`
     - public_local: `genome-local-<geohash>`
     - public_topic: `genome-topic-<slug>`
     - public_global: `genome-global`

2. **libp2p_mesh.ts extension** — multi-topic subscribe/unsubscribe/publish
   - Add `subscribeTopic(topic, handler)` method
   - Add `unsubscribeTopic(topic)` method
   - Add `publishToTopic(topic, payload)` method
   - Current mesh only handles single pubsubTopic

3. **mesh_bridge.ts extension** — multi-topic commands + signature verification
   - New commands: `join_network`, `leave_network`, `network_broadcast`
   - On receive: verify envelope signature before forwarding to Python
   - Drop messages that fail verification (Byzantine filter)

4. **geolocation.py** (GenomeUI backend) — IP geolocation + geohash
   - Call free IP geolocation API (ipapi.co or ip-api.com)
   - Convert lat/lng to geohash (level 4 ~40km radius)
   - Cache result, use as local network topic ID

5. **identity.py extension** — add `sign(message_bytes)` method
   - Use stored ed25519 private key to sign mesh messages

6. **mesh_bridge.py extension** — network join/leave/broadcast commands
   - `join_network(type, id, topic)` → sends cmd to bridge
   - `leave_network(topic)` → sends cmd to bridge
   - `network_broadcast(topic, envelope_dict)` → sends signed envelope to bridge
   - New event handler: `network_message` events from bridge

7. **main.py additions** — /api/networks endpoints
   - `GET /api/networks` — list joined networks
   - `POST /api/networks/join` — {type, id?, name?}
   - `POST /api/networks/leave` — {networkId}
   - `POST /api/networks/message` — {networkId, payload}
   - `GET /api/networks/{networkId}/messages` — recent messages

## Key technical decisions made

### @libp2p/crypto API (confirmed working)
- `generateKeyPairFromSeed('Ed25519', seedBytes)` → privateKey with `.sign(bytes)`
- `publicKey.raw` → raw 32-byte public key
- `publicKeyFromRaw('Ed25519', bytes)` → publicKey with `.verify(msg, sig)`
- All available via `@libp2p/crypto/keys` (ESM, use dynamicImport in TS)

### Byzantine filter approach
- Verify on receive in TypeScript (mesh_bridge.ts) before forwarding to Python
- Unsigned or invalid-sig messages dropped silently
- Timestamp replay window: 5 minutes

### Identity model
- Per-user (not per-device)
- Device pairing: mesh transfer (mDNS finds devices, key transfers over noise-encrypted channel)
- QR code fallback for no-LAN scenarios
- Recovery: 24-word BIP39 mnemonic
- Key storage: OS keychain (Windows Credential Manager / macOS Keychain / Linux Secret Service)

## File locations
- `C:\Users\steve\Documents\Nous\src\network\genome_envelope.ts` — new
- `C:\Users\steve\Documents\Nous\src\network\libp2p_mesh.ts` — modified
- `C:\Users\steve\Documents\Nous\src\scripts\mesh_bridge.ts` — modified
- `C:\Users\steve\Documents\Nous\package.json` — @libp2p/mdns added
- `C:\Users\steve\Documents\GenomeUI\backend\identity.py` — new
- `C:\Users\steve\Documents\GenomeUI\backend\mesh_bridge.py` — new + modified
- `C:\Users\steve\Documents\GenomeUI\backend\main.py` — modified

## Next session: resume network_registry.ts

