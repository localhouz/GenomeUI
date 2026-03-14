---
tags: ["architecture", "philosophy", "content-model", "versioning"]
category: decisions
created: 2026-02-28T16:12:38.882468
---

# GenomeUI — Core Philosophy & Content Model

# GenomeUI — Core Philosophy & Content Model

## Core Philosophy
GenomeUI is a **generative OS**. There are no apps to launch. Every experience is generated on demand by the agent and rendered as a scene. The OS IS the application layer. The vision: replace the entire app ecosystem — not by owning data silos, but by surfacing any service's data as a GenomeUI experience. Integrations are pipes; the rendered scene is the product.

## Content Model — Git-Style, Flat Namespace
- Content has **meaningful names** ("Q4 Report", "Budget 2026") — no nested folder hierarchy
- **Version history is automatic** — every generation/edit is a commit, no manual saving
- **Flat namespace** — content is found by name + semantic search + recency, not file paths
- **Branching** = divergent versions of the same content ("Q4 Report (draft)" vs "final")
- **Merging** = cross-device sync (phone session + desktop session reconcile like git)
- Internal addressing is hash-based (existing revision system in main.py) but invisible to user
- The session graph IS the repository — `revision`, `baseRevision`, journal = git internals

