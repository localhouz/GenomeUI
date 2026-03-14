---
tags: ["architecture", "intelligence", "routing", "nous", "llm", "embedded-model"]
category: decisions
created: 2026-02-28T16:12:47.160801
---

# GenomeUI — Intelligence Layer & Routing Architecture

# GenomeUI — Intelligence Layer & Routing Architecture

## Intelligence Layer
- A **purpose-trained local model** (fine-tuned on GenomeUI taxonomy) is the goal — not Ollama, not general LLMs, not external HTTP calls
- Training data = the TAXONOMY + synthetically generated labeled examples
- Model runs embedded in the Python backend via llama-cpp-python (no external process)
- Base model candidate: Qwen2.5-7B or Llama 3.1 8B, fine-tuned down to ~1-3B
- **Nous gateway** is a transitional bridge until the embedded model is ready
- The model must be agentic: multi-step reasoning, tool dispatch, context awareness

## Routing Architecture
- All traffic: Frontend (5173) → Nous gateway (7700) → Backend (8787) [transitional]
- `/api/auth` bypasses Nous — proxied directly to backend (8787) in vite.config.js
- Target: Frontend → Backend (with embedded model inside — no gateway layer)
- `body.nousIntent` from gateway takes priority over `classify_async` in turn handler
- `NOUS_URL=""` in backend — gateway handles classification, backend executes
- Rule-based semantics (semantics.py) = fallback only, not primary routing

