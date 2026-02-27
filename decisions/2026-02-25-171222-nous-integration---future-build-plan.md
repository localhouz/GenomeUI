---
tags: ["nous", "mesh", "reticulum", "future", "integration"]
category: decisions
created: 2026-02-25T17:12:22.728811
---

# Nous Integration - Future Build Plan

# Nous App - Mesh Network Integration (Future)

**Path:** C:\Users\steve\Documents\Nous

## What it is
Decentralized edge-first AI framework. Rust core with local LLM (Phi-3/Ollama), 384-dim vector memory, P2P mesh via libp2p/GossipSub. Every device is a "neuron." Semantic routing, ED25519 identity, EigenTrust++ reputation.

## Current transports (actual, not docs)
- TCP via libp2p ✅
- WebSocket (Tauri UI) ✅  
- In-memory (testing) ✅
- BLE/Bluetooth ❌ not built despite docs saying so
- Everything else ❌

## Integration with GenomeUI
1. Nous as local intelligence layer under the intent router
2. Mesh syncs session.graph / lastExecution across devices
3. Warning intelligence feeds into GenomeUI intents (NWS alerts → weather intent)
4. Reticulum bridge for off-grid LoRa/radio layer

## Architecture refactor needed first
Add `trait MeshTransport` + `TransportMux` for runtime-composable multi-medium routing. Currently compile-time feature flags only.

## Transport build priority
1. libp2p QUIC + WebRTC + Circuit Relay v2 (already in dependency)
2. BLE via btleplug crate
3. Reticulum sidecar bridge (Python rns → TCP → Rust)
4. Meshtastic serial bridge (LoRa hardware)
5. Satellite, acoustic (exotic)

## User goal
"Disruptive tech" — mesh that works over ANY available medium, auto-routes over best one.

