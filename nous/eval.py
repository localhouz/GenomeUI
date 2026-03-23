"""
nous/eval.py
------------
Evaluate a trained Nous GGUF model for:
  - Held-out taxonomy accuracy
  - JSON parse rate
  - Held-out general conversation behavior
  - MMLU-subset retention
  - Latency

Usage:
  python nous/eval.py --model nous/nous-qwen2.5-0.5b-genomeui-q4_k_m.gguf
  python nous/eval.py --model ... --baseline-mmlu 0.42
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

DATASET = Path(__file__).parent / "dataset.slim.jsonl"
MMLU_SUBSET = Path(__file__).parent / "mmlu_subset.jsonl"
REQUIRED_KEYS = {"response", "ops", "followUp", "clarify"}


def load_model(model_path: str, ctx: int, threads: int):
    try:
        from llama_cpp import Llama
    except ImportError:
        sys.exit("llama-cpp-python not installed. Run: pip install llama-cpp-python")
    return Llama(model_path=model_path, n_ctx=ctx, n_threads=threads, verbose=False)


def _build_chatml_prompt(messages: list[dict]) -> str:
    parts = []
    for message in messages:
        parts.append(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


def run_inference(llm, messages: list[dict], max_tokens: int = 256) -> tuple[str, float]:
    prompt = _build_chatml_prompt(messages)
    t0 = time.perf_counter()
    out = llm(prompt, max_tokens=max_tokens, stop=["<|im_end|>", "<|endoftext|>"], echo=False)
    ms = (time.perf_counter() - t0) * 1000
    return str(out["choices"][0]["text"]).strip(), ms


def parse_output(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return obj if REQUIRED_KEYS.issubset(obj.keys()) else None


def strip_last_assistant(messages: list[dict]) -> list[dict]:
    idx = len(messages) - 1
    while idx >= 0 and messages[idx]["role"] == "assistant":
        idx -= 1
    return messages[:idx + 1]


def load_dataset_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_rows(rows: list[dict], split: str, categories: set[str], sample: int) -> list[dict]:
    selected = []
    for row in rows:
        meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
        if str(meta.get("split", "train")) != split:
            continue
        if categories and str(meta.get("category", "")) not in categories:
            continue
        selected.append(row)
    return selected[:sample] if sample > 0 else selected


def expected_first_op(row: dict) -> str | None:
    messages = row.get("messages", [])
    if not messages:
        return None
    try:
        obj = json.loads(messages[-1]["content"])
    except Exception:
        return None
    ops = obj.get("ops") or []
    return str(ops[0].get("type", "")) if ops else ""


def run_json_eval(llm, rows: list[dict]) -> dict:
    parse_ok = 0
    op_match = 0
    general_ok = 0
    taxonomy_total = 0
    general_total = 0
    latencies: list[float] = []

    for row in rows:
        raw, latency_ms = run_inference(llm, strip_last_assistant(row.get("messages", [])))
        latencies.append(latency_ms)
        parsed = parse_output(raw)
        if parsed is None:
            continue
        parse_ok += 1
        predicted_ops = parsed.get("ops") or []
        predicted_first = str(predicted_ops[0].get("type", "")) if predicted_ops else ""
        expected = expected_first_op(row)
        meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
        category = str(meta.get("category", ""))

        if category.startswith("taxonomy_"):
            taxonomy_total += 1
            if expected is not None and predicted_first == expected:
                op_match += 1
        else:
            general_total += 1
            if expected == "" and predicted_first == "":
                general_ok += 1

    latencies.sort()
    n = len(latencies)
    return {
        "count": len(rows),
        "parse_rate": parse_ok / max(len(rows), 1),
        "taxonomy_accuracy": op_match / max(taxonomy_total, 1),
        "general_accuracy": general_ok / max(general_total, 1),
        "taxonomy_total": taxonomy_total,
        "general_total": general_total,
        "p50_ms": latencies[n // 2] if n else 0.0,
        "p95_ms": latencies[min(n - 1, int(n * 0.95))] if n else 0.0,
    }


def load_mmlu_subset(path: Path, sample: int) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:sample] if sample > 0 else rows


def _build_mmlu_prompt(row: dict) -> list[dict]:
    question = str(row.get("question", "")).strip()
    choices = row.get("choices", {}) if isinstance(row.get("choices"), dict) else {}
    prompt = (
        "Answer the following multiple-choice question with the single best letter only.\n\n"
        f"Question: {question}\n"
        f"A. {choices.get('A', '')}\n"
        f"B. {choices.get('B', '')}\n"
        f"C. {choices.get('C', '')}\n"
        f"D. {choices.get('D', '')}\n"
    )
    return [{"role": "user", "content": prompt}]


def _parse_letter(text: str) -> str:
    match = re.search(r"\b([ABCD])\b", text.upper())
    return match.group(1) if match else ""


def run_mmlu_eval(llm, rows: list[dict]) -> dict:
    correct = 0
    latencies: list[float] = []
    for row in rows:
        raw, latency_ms = run_inference(llm, _build_mmlu_prompt(row), max_tokens=16)
        latencies.append(latency_ms)
        if _parse_letter(raw) == str(row.get("answer", "")).strip().upper():
            correct += 1
    latencies.sort()
    n = len(latencies)
    return {
        "count": len(rows),
        "accuracy": correct / max(len(rows), 1),
        "p50_ms": latencies[n // 2] if n else 0.0,
        "p95_ms": latencies[min(n - 1, int(n * 0.95))] if n else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Path to GGUF model")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--mmlu-file", default=str(MMLU_SUBSET))
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--mmlu-sample", type=int, default=32)
    parser.add_argument("--ctx", type=int, default=1536)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--baseline-mmlu", type=float, default=0.0,
                        help="Base-model MMLU-subset accuracy for retention comparison.")
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        sys.exit(f"Model not found: {model_path}")

    dataset_rows = load_dataset_rows(Path(args.dataset))
    eval_rows = select_rows(
        dataset_rows,
        split="test",
        categories={"taxonomy_simple", "taxonomy_context", "general_qa", "general_multiturn", "general_query", "general_identity"},
        sample=int(args.sample),
    )
    mmlu_rows = load_mmlu_subset(Path(args.mmlu_file), int(args.mmlu_sample))

    llm = load_model(str(model_path), args.ctx, args.threads)

    print(f"Evaluating {model_path.name}")
    print(f"Held-out JSON rows: {len(eval_rows)}")
    print(f"MMLU subset rows:   {len(mmlu_rows)}")

    json_metrics = run_json_eval(llm, eval_rows)
    mmlu_metrics = run_mmlu_eval(llm, mmlu_rows)

    print("\nJSON Eval")
    print(f"  Parse rate:         {json_metrics['parse_rate']:.1%}")
    print(f"  Taxonomy accuracy:  {json_metrics['taxonomy_accuracy']:.1%}  (n={json_metrics['taxonomy_total']})")
    print(f"  General accuracy:   {json_metrics['general_accuracy']:.1%}  (n={json_metrics['general_total']})")
    print(f"  Latency P50/P95:    {json_metrics['p50_ms']:.0f} / {json_metrics['p95_ms']:.0f} ms")

    print("\nMMLU Subset")
    print(f"  Accuracy:           {mmlu_metrics['accuracy']:.1%}  (n={mmlu_metrics['count']})")
    print(f"  Latency P50/P95:    {mmlu_metrics['p50_ms']:.0f} / {mmlu_metrics['p95_ms']:.0f} ms")

    retention_ok = True
    if args.baseline_mmlu > 0:
        floor = max(0.0, args.baseline_mmlu - 0.05)
        retention_ok = mmlu_metrics["accuracy"] >= floor
        print(f"  Retention floor:    {floor:.1%}  (baseline {args.baseline_mmlu:.1%})")

    success = (
        json_metrics["parse_rate"] >= 0.95
        and json_metrics["taxonomy_accuracy"] >= 0.90
        and json_metrics["p95_ms"] <= 500
        and retention_ok
    )

    if not success:
        if json_metrics["parse_rate"] < 0.95:
            print(f"FAIL parse rate below target: {json_metrics['parse_rate']:.1%} < 95%")
        if json_metrics["taxonomy_accuracy"] < 0.90:
            print(f"FAIL taxonomy accuracy below target: {json_metrics['taxonomy_accuracy']:.1%} < 90%")
        if json_metrics["p95_ms"] > 500:
            print(f"FAIL latency above target: {json_metrics['p95_ms']:.0f}ms > 500ms")
        if not retention_ok:
            print("FAIL general capability regressed more than 5% from baseline")
        return 1

    print("\nAll primary targets met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
