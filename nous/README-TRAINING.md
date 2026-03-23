# Nous Training

Nous is the general AI assistant behind GenomeUI. The fine-tuned model must stay useful for normal conversation, coding help, writing, and reasoning while learning GenomeUI's intent vocabulary and JSON action format.

## Current target

- Base model: `Qwen/Qwen2.5-0.5B-Instruct`
- Fine-tune method: LoRA / QLoRA on Modal
- Export: GGUF `Q4_K_M`
- Primary artifact name: `nous-qwen2.5-0.5b-genomeui-q4_k_m.gguf`

This smaller base is the preferred path for CPU classification latency. It is substantially easier to keep under the Tier 0 / Tier 1 latency budget than the older 3B path.

## Dataset

Generate the corpus with:

```bash
python nous/gen_dataset.py --out nous/dataset.jsonl
```

Outputs:

- `nous/dataset.jsonl`: full training rows with `messages`, `tools`, and `meta`
- `nous/dataset.slim.jsonl`: compact training rows for Modal upload

The generator now guarantees:

- every taxonomy intent gets 5-10 prompt variants
- deterministic `train` / `test` split metadata
- taxonomy, clarify, context, compound, general QA, general query, identity, and multi-turn categories

The training script uses `meta.split` so held-out examples are not leaked into training.

## Modal training

Install and authenticate once:

```bash
pip install modal
modal setup
```

Run training:

```bash
modal run nous/train_modal.py
```

Useful variants:

```bash
modal run nous/train_modal.py --epochs 1
modal run nous/train_modal.py --base-model Qwen/Qwen2.5-0.5B-Instruct --epochs 3
```

The script:

1. Uploads `dataset.slim.jsonl`
2. Fine-tunes the base model with LoRA
3. Respects the dataset's held-out split for eval during training
4. Merges weights
5. Exports BF16 GGUF
6. Quantizes to `Q4_K_M`
7. Writes an Ollama `Modelfile`

Local outputs after a successful run:

- `nous/nous-qwen2.5-0.5b-genomeui-q4_k_m.gguf`
- `nous/Modelfile`

## Ollama import

After training:

```bash
cd nous
ollama create genomeui-nous -f Modelfile
ollama run genomeui-nous
```

That imported model is the artifact intended for the Nous Rust gateway / local inference path.

## Evaluation

Install local eval dependency:

```bash
pip install llama-cpp-python
```

Run eval:

```bash
python nous/eval.py --model nous/nous-qwen2.5-0.5b-genomeui-q4_k_m.gguf
```

If you have the base-model MMLU-subset score, pass it to enforce the "within 5%" retention rule:

```bash
python nous/eval.py --model nous/nous-qwen2.5-0.5b-genomeui-q4_k_m.gguf --baseline-mmlu 0.42
```

`nous/eval.py` reports:

- held-out JSON parse rate
- held-out taxonomy accuracy
- held-out general conversation behavior
- MMLU-subset accuracy from `nous/mmlu_subset.jsonl`
- latency P50 / P95

Primary targets:

- taxonomy accuracy: `>= 90%`
- JSON parse rate: `>= 95%`
- held-out latency P95: `<= 500ms`
- MMLU-subset retention: no more than `5%` below the base-model baseline

## Notes

- `nous/gen_dataset.py` is the source of truth for dataset shape.
- `nous/mmlu_subset.jsonl` is a lightweight retention harness, not a full benchmark suite.
- If the model starts overfitting into a classifier, increase the share of `general_qa`, `general_multiturn`, and identity-preserving examples before increasing rank or epochs.
