"""
backend/nous_loader.py
----------------------
Embedded Nous model loader for GenomeUI.

Loads the fine-tuned Qwen2.5-3B GGUF (nous/nous-3b-q4.gguf) at startup and
exposes a single async function `classify` that replaces the Nous gateway
for classification + response generation.

The model returns JSON in the format trained by nous/gen_dataset.py:
  {"response": "...", "ops": [{"type": "...", "slots": {...}}, ...],
   "followUp": bool, "clarify": null | "..."}

Import-safe: if llama-cpp-python is not installed or the GGUF is missing,
everything degrades gracefully (returns None).
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

_log = logging.getLogger("genomeui.nous")

_GGUF_PATH = pathlib.Path(__file__).parent.parent / "nous" / "nous-3b-q4.gguf"

_model = None  # type: Any  # llama_cpp.Llama | None

_SYSTEM_PROMPT = (
    "You are Nous, the intelligence layer of GenomeUI — a personal operating system. "
    "Your manner is that of a supremely capable personal aide: precise, anticipatory, "
    "occasionally dry, never fawning. You speak plainly and act decisively.\n\n"
    "You have access to the user's full session context: their tasks, notes, calendar, "
    "files, messages, and connected services. You use this context without being asked.\n\n"
    "You MUST always respond with valid JSON in exactly this shape:\n"
    '{"response": "<string>", "ops": [{"type": "<op>", "slots": {}}], "followUp": <bool>, "clarify": <null|string>}\n\n'
    "Rules:\n"
    "- response: 1-2 sentences, in character. Always present.\n"
    "- ops: array of capability ops to execute. Empty array if none.\n"
    "- followUp: true if more input is expected.\n"
    "- clarify: null unless genuinely ambiguous — then one precise question.\n"
    "- Never use clarify and ops together.\n"
    "- Do not explain what you are doing. Just do it.\n"
    "- Dry wit permitted. Sycophancy is not."
)


def load() -> bool:
    """Attempt to load the GGUF model. Called once at startup."""
    global _model

    if not _GGUF_PATH.exists():
        _log.info("Nous GGUF not found at %s — using rule-based classifier", _GGUF_PATH)
        return False

    try:
        from llama_cpp import Llama  # type: ignore[import]
    except ImportError:
        _log.warning(
            "llama-cpp-python not installed — embedded Nous model unavailable. "
            "Run: pip install llama-cpp-python"
        )
        return False

    try:
        _log.info("Loading Nous model from %s …", _GGUF_PATH)
        _model = Llama(
            model_path=str(_GGUF_PATH),
            n_ctx=2048,
            n_threads=4,
            n_gpu_layers=0,  # CPU-only (Arc iGPU has no llama.cpp GPU support yet)
            verbose=False,
        )
        _log.info("Nous model loaded")
        return True
    except Exception as exc:
        _log.error("Failed to load Nous GGUF: %s", exc)
        _model = None
        return False


def is_loaded() -> bool:
    return _model is not None


def _call_model(user_text: str) -> dict[str, Any] | None:
    """Synchronous inference. Runs in a thread via asyncio.to_thread."""
    if _model is None:
        return None

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_text},
    ]

    try:
        result = _model.create_chat_completion(
            messages=messages,
            max_tokens=256,
            temperature=0.0,
            stop=None,
        )
        raw = result["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        _log.warning("Nous model inference failed: %s", exc)
        return None

    # Strip markdown code fences if the model wraps its JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Nous returned non-JSON: %r", raw[:120])
        return None

    return parsed if isinstance(parsed, dict) else None


async def classify(user_text: str) -> dict[str, Any] | None:
    """
    Async classify. Returns:
      {"ops": [{"type": str, "slots": dict}, ...],
       "response": str, "followUp": bool, "clarify": str | None}
    or None if the model is unavailable or inference fails.
    """
    if _model is None:
        return None

    result = await asyncio.to_thread(_call_model, user_text)
    if result is None:
        return None

    ops_raw = result.get("ops") or []
    if not isinstance(ops_raw, list):
        ops_raw = []

    clean_ops = []
    for op in ops_raw:
        if not isinstance(op, dict):
            continue
        op_type = str(op.get("type") or op.get("op") or "").strip().lower()
        if not op_type:
            continue
        slots = op.get("slots") or {}
        if not isinstance(slots, dict):
            slots = {}
        clean_ops.append({"type": op_type, "slots": slots})

    return {
        "ops": clean_ops,
        "response": str(result.get("response") or ""),
        "followUp": bool(result.get("followUp", False)),
        "clarify": result.get("clarify") or None,
    }
