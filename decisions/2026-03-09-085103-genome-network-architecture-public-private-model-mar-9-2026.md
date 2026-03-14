---
tags: ["mesh", "network", "architecture", "public", "private", "warnings", "identity"]
category: decisions
created: 2026-03-09T08:51:03.005425
---

# Genome Network Architecture Public Private Model Mar 9 2026

# Genome Network Architecture — Public/Private Model

## Network Types

### Public — open join
- **Global mesh** — anyone on Genome can participate, PSA/warning broadcasts, community announcements
- **Local mesh** — geographically bounded, people near you, neighborhood alerts, local groups
  - Discovery: IP geolocation (no permission needed) + opt-in GPS for precision
- **Topic groups** — open groups anyone can find and join, like chat rooms with a subject

### Private — controlled access
- **P2P** — direct encrypted channel between two identity-verified users
- **Private groups** — invite-only, end-to-end encrypted, membership controlled by creator

## Local Detection Decision
- **mDNS** → personal device mesh (same WiFi/LAN), automatic zero-config
- **IP geolocation + opt-in GPS** → community/public local networks (neighborhood level)
- These are complementary, not competing

## Warning / PSA System
- Already started in Nous Rust:
  - rust/src/warning.rs — warning engine, confidence scoring, severity levels, source verification, rule calibration
  - rust/src/realtime_feed.rs — NWS weather alert integration
- Extend to: community-generated PSAs, emergency broadcasts
- Trust system (EigenTrust++) filters warnings by source reputation

## Nous Mesh Tech — Production-Ready vs Needs Work

### Production-ready:
- libp2p mesh (TCP + noise + gossipsub) — TS and Rust
- Trust system (EigenTrust++) — TS and Rust
- Security/privacy (metadata redaction, differential privacy, poisoning detection)
- Data layer (FileBackedDataLayer TS, sled Rust)
- Semantic subscriptions, agent registry, agent protocols
- Warning engine (Rust)

### Critical gaps — before public launch:
1. mDNS peer discovery — MISSING, add @libp2p/mdns to libp2p_mesh.ts — DO THIS NEXT
2. Persistent identity (DID) — foundation for everything public
3. Network types as first-class concepts — public/private/group/P2P in mesh layer
4. IP geolocation routing — local mesh discovery without GPS
5. Byzantine fault tolerance — adversarial peers on public networks
6. Sybil detection — fake identity prevention
7. End-to-end encryption for private — keys never leave devices
8. Group membership and access control — invite system, moderation

### Later (post-launch):
- RLNC coding, capability markets, ActivityPub federation
- Advanced differential privacy, image/cross-modal embeddings
- Model distillation, curriculum learning

## Build Priority Order
1. mDNS discovery — personal device mesh works automatically
2. Persistent DID identity — foundation for public network model
3. Network type layer (public/private/group/P2P)
4. IP geolocation for local mesh
5. Byzantine resilience and Sybil detection
6. Warning/PSA scenes in GenomeUI
7. E2E encryption for private channels
8. Group access control

## Architecture Note
Rust implementation is the right production foundation.
TypeScript mesh bridge is the right GenomeUI integration point now.
Long-term: Rust core IS the mesh, TypeScript bridge becomes thin wrapper or is retired.

