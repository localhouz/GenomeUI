"""
nous/train_modal.py
-------------------
LoRA fine-tuning for the GenomeUI Nous assistant on Modal.

Default target:
  - Base model: Qwen/Qwen2.5-0.5B-Instruct
  - Output:      GGUF Q4_K_M for Ollama / llama.cpp

Usage:
  modal run nous/train_modal.py
  modal run nous/train_modal.py --epochs 1
  modal run nous/train_modal.py --base-model Qwen/Qwen2.5-0.5B-Instruct --epochs 3
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import modal

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_OUTPUT_BASENAME = "nous-qwen2.5-0.5b-genomeui"
DEFAULT_MAX_SEQ_LEN = 1536
DEFAULT_LORA_RANK = 32
DEFAULT_EPOCHS = 3

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "git", "cmake", "ninja-build", "curl", "build-essential", "libcurl4-openssl-dev",
    )
    .pip_install(
        "unsloth[colab-new]",
        "trl>=0.7.4",
        "peft>=0.7.0",
        "accelerate>=0.25.0",
        "bitsandbytes>=0.41.0",
        "datasets>=2.16.0",
        "transformers>=4.37.0",
        "torch>=2.1.0",
        "huggingface_hub",
        "sentencepiece",
        "protobuf",
    )
    .run_commands(
        "git clone --depth 1 https://github.com/ggerganov/llama.cpp /root/llama.cpp",
        "cmake -B /root/llama.cpp/build /root/llama.cpp -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -GNinja",
        "cmake --build /root/llama.cpp/build --target llama-quantize",
        "cp /root/llama.cpp/build/bin/llama-quantize /root/llama.cpp/llama-quantize",
        "pip install /root/llama.cpp/gguf-py",
    )
)

app = modal.App("nous-finetune", image=image)
volume = modal.Volume.from_name("nous-model-cache", create_if_missing=True)


def _chat_rows_from_jsonl(dataset_jsonl: bytes) -> tuple[list[dict], list[dict]]:
    rows = [json.loads(line) for line in dataset_jsonl.decode("utf-8").splitlines() if line.strip()]
    train_rows: list[dict] = []
    eval_rows: list[dict] = []
    for row in rows:
        meta = row.get("meta", {}) if isinstance(row.get("meta"), dict) else {}
        split = str(meta.get("split", "train") or "train").strip().lower()
        if split == "test":
            eval_rows.append(row)
        else:
            train_rows.append(row)
    if not eval_rows:
        cutoff = max(1, int(len(rows) * 0.1))
        eval_rows = rows[:cutoff]
        train_rows = rows[cutoff:]
    return train_rows, eval_rows


def render_ollama_modelfile(model_filename: str, base_model: str, max_seq_len: int) -> str:
    return (
        f"FROM ./{model_filename}\n"
        f'PARAMETER num_ctx {int(max_seq_len)}\n'
        'PARAMETER temperature 0.2\n'
        'PARAMETER top_p 0.9\n'
        f'SYSTEM "Fine-tuned from {base_model} for GenomeUI Nous."\n'
    )


@app.function(
    gpu="A100",
    timeout=60 * 90,
    volumes={"/cache": volume},
)
def train(
    dataset_jsonl: bytes,
    base_model: str = DEFAULT_BASE_MODEL,
    epochs: int = DEFAULT_EPOCHS,
    max_seq_len: int = DEFAULT_MAX_SEQ_LEN,
    lora_rank: int = DEFAULT_LORA_RANK,
    output_basename: str = DEFAULT_OUTPUT_BASENAME,
) -> bytes:
    import gc
    import shutil
    import subprocess

    import torch
    from datasets import Dataset
    from transformers import TrainingArguments
    from trl import SFTTrainer
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template

    os.environ["HF_HOME"] = "/cache/hf"

    train_rows, eval_rows = _chat_rows_from_jsonl(dataset_jsonl)
    print(f"Dataset rows: train={len(train_rows)} eval={len(eval_rows)} base_model={base_model}")

    train_ds = Dataset.from_list(train_rows)
    eval_ds = Dataset.from_list(eval_rows)

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model,
        max_seq_length=int(max_seq_len),
        load_in_4bit=True,
        dtype=None,
        cache_dir="/cache/hf",
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r=int(lora_rank),
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_alpha=int(lora_rank) * 2,
        lora_dropout=0.05,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    tokenizer = get_chat_template(tokenizer, chat_template="qwen-2.5")

    def format_batch(batch: dict) -> dict:
        return {
            "text": [
                tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
                for messages in batch["messages"]
            ]
        }

    train_ds = train_ds.map(format_batch, batched=True)
    eval_ds = eval_ds.map(format_batch, batched=True)

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        dataset_text_field="text",
        max_seq_length=int(max_seq_len),
        dataset_num_proc=4,
        packing=True,
        args=TrainingArguments(
            per_device_train_batch_size=8,
            gradient_accumulation_steps=2,
            warmup_steps=25,
            num_train_epochs=int(epochs),
            learning_rate=3e-4,
            bf16=torch.cuda.is_bf16_supported(),
            fp16=not torch.cuda.is_bf16_supported(),
            logging_steps=10,
            eval_strategy="steps",
            eval_steps=150,
            save_strategy="no",
            optim="adamw_8bit",
            weight_decay=0.01,
            lr_scheduler_type="cosine",
            seed=42,
            output_dir="/tmp/nous-checkpoints",
            report_to="none",
        ),
    )

    stats = trainer.train()
    print(f"Training done. Runtime: {stats.metrics.get('train_runtime', 0.0):.1f}s")

    merged_dir = Path(f"/tmp/{output_basename}-merged")
    print("Merging LoRA weights to 16-bit...")
    model.save_pretrained_merged(str(merged_dir), tokenizer, save_method="merged_16bit")
    tokenizer.save_pretrained(str(merged_dir))

    from transformers import AutoConfig

    AutoConfig.from_pretrained(base_model, cache_dir="/cache/hf").save_pretrained(str(merged_dir))

    print("Freeing model before GGUF conversion...")
    del model, tokenizer, trainer, train_ds, eval_ds
    gc.collect()
    torch.cuda.empty_cache()

    os.chdir("/root")
    bf16_gguf = Path(f"/tmp/{output_basename}-bf16.gguf")
    q4_gguf = Path(f"/tmp/{output_basename}-q4_k_m.gguf")

    print("Converting to BF16 GGUF...")
    subprocess.run(
        ["python", "llama.cpp/convert_hf_to_gguf.py", str(merged_dir), "--outfile", str(bf16_gguf), "--outtype", "bf16"],
        check=True,
    )
    print("Quantizing to Q4_K_M...")
    subprocess.run(
        ["/root/llama.cpp/llama-quantize", str(bf16_gguf), str(q4_gguf), "Q4_K_M"],
        check=True,
    )

    dest = Path("/cache") / q4_gguf.name
    shutil.copy(q4_gguf, dest)
    (Path("/cache") / f"{output_basename}.Modelfile").write_text(
        render_ollama_modelfile(q4_gguf.name, base_model, int(max_seq_len)),
        encoding="utf-8",
    )
    print(f"Saved artifacts to volume: {dest}")
    return q4_gguf.read_bytes()


@app.local_entrypoint()
def main(
    epochs: int = DEFAULT_EPOCHS,
    base_model: str = DEFAULT_BASE_MODEL,
    max_seq_len: int = DEFAULT_MAX_SEQ_LEN,
    lora_rank: int = DEFAULT_LORA_RANK,
    output_basename: str = DEFAULT_OUTPUT_BASENAME,
):
    dataset_path = Path(__file__).parent / "dataset.slim.jsonl"
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}")
        print("Run: python nous/gen_dataset.py")
        sys.exit(1)

    size_mb = dataset_path.stat().st_size / 1_048_576
    print(f"Uploading dataset: {size_mb:.1f} MB")
    print(f"Training {base_model} on A100 for {epochs} epoch(s)...")

    gguf_bytes = train.remote(
        dataset_path.read_bytes(),
        base_model=base_model,
        epochs=epochs,
        max_seq_len=max_seq_len,
        lora_rank=lora_rank,
        output_basename=output_basename,
    )

    out = Path(__file__).parent / f"{output_basename}-q4_k_m.gguf"
    out.write_bytes(gguf_bytes)
    modelfile = Path(__file__).parent / "Modelfile"
    modelfile.write_text(render_ollama_modelfile(out.name, base_model, max_seq_len), encoding="utf-8")
    print(f"Done. Model saved to {out}")
    print(f"Ollama Modelfile saved to {modelfile}")


@app.local_entrypoint()
def download(output_basename: str = DEFAULT_OUTPUT_BASENAME):
    @app.function(volumes={"/cache": volume})
    def _read_artifacts(name: str) -> tuple[bytes, str]:
        gguf_path = Path("/cache") / f"{name}-q4_k_m.gguf"
        modelfile_path = Path("/cache") / f"{name}.Modelfile"
        if not gguf_path.exists():
            raise FileNotFoundError(f"GGUF not found in volume: {gguf_path}")
        modelfile = modelfile_path.read_text(encoding="utf-8") if modelfile_path.exists() else ""
        return gguf_path.read_bytes(), modelfile

    data, modelfile_text = _read_artifacts.remote(output_basename)
    out = Path(__file__).parent / f"{output_basename}-q4_k_m.gguf"
    out.write_bytes(data)
    if modelfile_text:
        (Path(__file__).parent / "Modelfile").write_text(modelfile_text, encoding="utf-8")
    print(f"Saved to {out}")
