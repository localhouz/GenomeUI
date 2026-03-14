---
tags: ["nous", "fine-tuning", "modal", "gguf", "training"]
category: decisions
created: 2026-03-07T13:07:24.854795
---

# Nous Fine-tuning Pipeline — Current State (Mar 2026)

# Nous Fine-tuning Pipeline — Current State

## Status
Training works (3 epochs, ~15min on A100, loss 0.19). GGUF export pipeline is the ongoing blocker. Two fixes applied this session, need to re-run.

## What's in place (all written, confirmed working)
- `nous/train_modal.py` — Modal A100 training + GGUF export
- `nous/gen_dataset.py` — generates 2,609 training examples → `dataset.slim.jsonl`
- `backend/nous_loader.py` — embedded loader (llama-cpp-python), graceful fallback
- `backend/main.py` — wired: startup loads GGUF, turn handler tries nous_loader first

## GGUF Export Pipeline (in train_modal.py)
1. `model.save_pretrained_merged(merged_dir, tokenizer, save_method="merged_16bit")`
2. `tokenizer.save_pretrained(merged_dir)` — writes all tokenizer files
3. `AutoConfig.from_pretrained(MODEL_ID, cache_dir="/cache/hf").save_pretrained(merged_dir)` — fixes broken config.json (model_type missing)
4. **FREE MEMORY**: `del model, tokenizer, trainer, train_ds, eval_ds, orig_config; gc.collect(); torch.cuda.empty_cache()` — CRITICAL, prevents OOM in subprocess
5. `python llama.cpp/convert_hf_to_gguf.py merged_dir --outfile bf16.gguf --outtype bf16`
6. `llama-quantize bf16.gguf q4.gguf Q4_K_M`
7. Copy to `/cache/nous-3b-q4.gguf` (Modal Volume), return bytes

## Errors fixed in this session
1. `tokenizer.model not found` → fixed by `tokenizer.save_pretrained(merged_dir)`
2. `'dict' has no attr 'model_type'` → fixed by overwriting config.json from `AutoConfig.from_pretrained`
3. `KeyboardInterrupt` during transformers import in subprocess → OOM, fixed by `del model; gc.collect(); torch.cuda.empty_cache()` before subprocess

## Next action
Re-run: `modal run --detach nous/train_modal.py::main`
Then download: `modal run nous/train_modal.py::download`
Then: `pip install llama-cpp-python` in .venv to activate embedded loader

## Modal Volume
- Name: `nous-model-cache`
- GGUF persists at `/cache/nous-3b-q4.gguf` after successful run
- Use `download` entrypoint to retrieve if client disconnects

## Key file paths
- `nous/train_modal.py` — training script
- `nous/dataset.slim.jsonl` — training data (3.2MB, already generated)
- `nous/nous-3b-q4.gguf` — output (doesn't exist yet)
- `backend/nous_loader.py` — embedded inference
- `backend/main.py` — wired, ready

