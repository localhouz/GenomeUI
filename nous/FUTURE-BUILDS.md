# Nous — Future Builds

## Multi-Modal (Vision)

**Why it's coming:** GenomeUI will need Nous to see — screen content, camera frames, documents, photos.

**The constraint:** Text-only GGUF fine-tunes are a dead end for vision. The architecture has to support it from the base model. Vision encoder weights are baked in at pretraining time and cannot be added after.

**The upgrade path:** `Qwen2.5-VL-3B-Instruct` — same family as the current base, same tokenizer lineage, same fine-tuning pipeline. One line changes in `train_modal.py`:

```python
MODEL_ID = "Qwen/Qwen2.5-VL-3B-Instruct"
```

The existing text dataset trains against it without modification. The door stays open.

**What it unlocks:**
- "What's on my screen?" — Nous sees the current scene
- Camera intent → Nous analyzes the frame directly
- Document scenes — Nous reads the PDF/image instead of relying on text extraction
- "What's this?" from the phone camera

**What the dataset needs when we get there:**
- Vision examples: `(image, question) → structured op + response`
- These cannot be synthetically generated — real image-text pairs required
- Scope: a few thousand labeled examples covering camera, screen, and document use cases

---

## Agentic Loop (Multi-Step Reasoning)

**Why it matters:** Today Nous is one-shot: intent → op list. A real agent needs to observe tool results and reason about next steps.

**What's needed:**
- Training data: full agentic traces — intent → plan → tool call → tool result → reasoning → next call → final response
- Output format: add a `"thought"` field for chain-of-thought before ops
- Backend: run Nous in a loop (call → execute → feed result back → call again) rather than one shot
- Scale: thousands of agentic loop examples, not hundreds

**Current state:** The backend already does multi-step execution (`execute_operations`, `_ref` slot resolution). The model just doesn't drive it — the planner does. The upgrade is making Nous the planner.

---

## True From-Scratch Training

**Why you'd want it:** A fine-tuned base model always carries someone else's pretraining. The weights are derivative.

**What it requires:**
- A small custom architecture (50–200M params) — Transformer encoder or SSM (Mamba/RWKV)
- Training from scratch on GenomeUI's taxonomy + synthetically generated data
- No derivative-works question; weights are 100% yours
- Inference: <10ms on CPU, fully offline

**Realistic scope:** For classification + structured output only (not general assistant). A purpose-built learned parser, not an LLM. The 253K taxonomy examples are sufficient seed data.

**Not realistic scope:** A general agent from scratch. That requires billions of tokens and millions in compute.
