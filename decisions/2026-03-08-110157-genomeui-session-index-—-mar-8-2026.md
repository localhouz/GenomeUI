---
tags: ["session-index", "genomeui", "build-log"]
category: decisions
created: 2026-03-08T11:01:57.411627
---

# GenomeUI Session Index — Mar 8 2026

# GenomeUI Session Index — Mar 8 2026

## What was built this session

### Infrastructure
- `dev.ps1`: fixed em-dash UTF-8/Windows-1252 encoding bug (string terminator crash)
- `dev.ps1`: Nous gateway made optional — starts without it, logs "embedded classifier"
- `vite.config.js`: all traffic → 8787 (backend direct), removed 7700 gateway dependency
- CSP updated: dropped ws://localhost:7700 and http://localhost:7700

### Nous Fine-tuning Pipeline (blocked on Modal billing)
- `nous/train_modal.py`: 3 fixes applied, ready to re-run when billing resets:
  1. `tokenizer.save_pretrained(merged_dir)` — writes all tokenizer files
  2. `AutoConfig.from_pretrained(MODEL_ID).save_pretrained(merged_dir)` — fixes broken config.json (model_type missing → AutoTokenizer crash)
  3. `del model, tokenizer, trainer; gc.collect(); torch.cuda.empty_cache()` — frees ~12GB before conversion subprocess (was OOM-killing transformers import)
- Re-run: `modal run --detach nous/train_modal.py::main`
- Download: `modal run nous/train_modal.py::download`
- After GGUF lands: `pip install llama-cpp-python` in .venv

### Frontend — 14 new scene renderers built
All three parts per scene: buildCoreSurface data handler, renderBlock HTML, CSS

Wave-3 missing (had canvas renderer, no HTML):
- smarthome — device grid, breathing orbs canvas, amber/teal
- travel — flight/hotel stream, dark blue
- payments — dollar hero, transaction list, purple
- focus — timer ring, SVG progress, deep green

Remaining domains (using makeComputerRenderer canvas):
- video — streaming catalog, purple gradient
- food_delivery — order status + progress bar, amber
- rideshare — destination + driver + ETA, blue
- camera — viewfinder with corner brackets, black
- photos — 3-column memory grid, dark
- clock — large digit display + SVG ring, dark grey
- recipe — ingredient list, olive green
- grocery — checklist + progress bar, dark green
- translate — source/result two-pane, dark blue
- book — title + reading progress + stream, dark red

Total scene renderers: 44 dedicated kinds

## Current state summary
- Backend write ops (gmail.send, gcal.create/update/delete, slack.send) — ALREADY IMPLEMENTED with live API. Just need OAuth token from user connecting accounts.
- All 355 taxonomy intents have dedicated or appropriate scene renderers
- Nous GGUF: blocked on Modal billing. All fixes in place.

## Next session priorities
1. Re-run Nous training when Modal billing resets
2. User needs to OAuth connect Google (Gmail/GCal) to activate live write ops
3. "Take over any screen" feature (see separate memory note)

