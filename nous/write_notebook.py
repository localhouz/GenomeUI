"""Helper script: writes nous_finetune.ipynb. Run once, then delete."""
import json

cells = []

def md(id_, *lines):
    cells.append({"cell_type": "markdown", "id": id_, "metadata": {}, "source": list(lines)})

def code(id_, *lines):
    cells.append({"cell_type": "code", "id": id_, "metadata": {}, "outputs": [], "source": list(lines)})

md("md-title",
   "# Nous Fine-Tuning — GenomeUI\n",
   "Fine-tunes **Qwen2.5-3B-Instruct** on the GenomeUI TAXONOMY dataset using QLoRA (unsloth).\n\n",
   "**Runtime:** GPU — T4 minimum, A100 recommended.\n\n",
   "Steps: Install → Upload dataset → Load model → Train → Export GGUF → Download\n")

code("cell-install",
     "# Cell 1 — Install (~3 min first run)\n",
     "!pip install -q unsloth trl peft accelerate bitsandbytes datasets\n",
     "!pip install -q --upgrade transformers\n")

code("cell-upload",
     "# Cell 2 — Upload nous/dataset.slim.jsonl\n",
     "from google.colab import files\n",
     "uploaded = files.upload()\n",
     "DATASET_PATH = list(uploaded.keys())[0]\n",
     "print(f'Uploaded: {DATASET_PATH}')\n")

code("cell-load-data",
     "# Cell 3 — Load dataset\n",
     "import json\n",
     "from datasets import Dataset\n",
     "\n",
     "with open(DATASET_PATH) as f:\n",
     "    rows = [json.loads(l) for l in f if l.strip()]\n",
     "\n",
     "print(f'Total examples: {len(rows)}')\n",
     "print('Sample user:', rows[0]['messages'][1]['content'])\n",
     "print('Sample asst:', rows[0]['messages'][2]['content'][:120])\n",
     "\n",
     "from datasets import Dataset\n",
     "dataset = Dataset.from_list(rows)\n",
     "split = dataset.train_test_split(test_size=0.05, seed=42)\n",
     "train_ds, eval_ds = split['train'], split['test']\n",
     "print(f'Train: {len(train_ds)}  Eval: {len(eval_ds)}')\n")

code("cell-load-model",
     "# Cell 4 — Load Qwen2.5-3B-Instruct with 4-bit QLoRA\n",
     "from unsloth import FastLanguageModel\n",
     "import torch\n",
     "\n",
     "MODEL_ID = 'Qwen/Qwen2.5-3B-Instruct'\n",
     "MAX_SEQ_LEN = 2048\n",
     "\n",
     "model, tokenizer = FastLanguageModel.from_pretrained(\n",
     "    model_name=MODEL_ID,\n",
     "    max_seq_length=MAX_SEQ_LEN,\n",
     "    load_in_4bit=True,\n",
     "    dtype=None,\n",
     ")\n",
     "\n",
     "model = FastLanguageModel.get_peft_model(\n",
     "    model,\n",
     "    r=16,\n",
     "    target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj',\n",
     "                    'gate_proj', 'up_proj', 'down_proj'],\n",
     "    lora_alpha=32,\n",
     "    lora_dropout=0.05,\n",
     "    bias='none',\n",
     "    use_gradient_checkpointing='unsloth',\n",
     "    random_state=42,\n",
     ")\n",
     "print('Model ready.')\n")

code("cell-format",
     "# Cell 5 — Apply Qwen2.5 chat template\n",
     "from unsloth.chat_templates import get_chat_template\n",
     "\n",
     "tokenizer = get_chat_template(tokenizer, chat_template='qwen-2.5')\n",
     "\n",
     "def format_example(batch):\n",
     "    texts = []\n",
     "    for msgs in batch['messages']:\n",
     "        text = tokenizer.apply_chat_template(\n",
     "            msgs, tokenize=False, add_generation_prompt=False)\n",
     "        texts.append(text)\n",
     "    return {'text': texts}\n",
     "\n",
     "train_ds = train_ds.map(format_example, batched=True)\n",
     "eval_ds  = eval_ds.map(format_example, batched=True)\n",
     "print(train_ds[0]['text'][:400])\n")

code("cell-train",
     "# Cell 6 — Train (3 epochs, ~45 min on T4, ~15 min on A100)\n",
     "from trl import SFTTrainer\n",
     "from transformers import TrainingArguments\n",
     "\n",
     "trainer = SFTTrainer(\n",
     "    model=model,\n",
     "    tokenizer=tokenizer,\n",
     "    train_dataset=train_ds,\n",
     "    eval_dataset=eval_ds,\n",
     "    dataset_text_field='text',\n",
     "    max_seq_length=MAX_SEQ_LEN,\n",
     "    dataset_num_proc=2,\n",
     "    packing=True,\n",
     "    args=TrainingArguments(\n",
     "        per_device_train_batch_size=4,\n",
     "        gradient_accumulation_steps=4,\n",
     "        warmup_steps=20,\n",
     "        num_train_epochs=3,\n",
     "        learning_rate=2e-4,\n",
     "        fp16=not torch.cuda.is_bf16_supported(),\n",
     "        bf16=torch.cuda.is_bf16_supported(),\n",
     "        logging_steps=10,\n",
     "        evaluation_strategy='steps',\n",
     "        eval_steps=100,\n",
     "        save_strategy='steps',\n",
     "        save_steps=200,\n",
     "        optim='adamw_8bit',\n",
     "        weight_decay=0.01,\n",
     "        lr_scheduler_type='cosine',\n",
     "        seed=42,\n",
     "        output_dir='nous-checkpoints',\n",
     "        report_to='none',\n",
     "    ),\n",
     ")\n",
     "\n",
     "stats = trainer.train()\n",
     "print(f'Done. Runtime: {stats.metrics[\"train_runtime\"]:.1f}s')\n")

code("cell-export",
     "# Cell 7 — Export GGUF Q4_K_M (for llama-cpp-python)\n",
     "model.save_pretrained_gguf('nous-gguf', tokenizer, quantization_method='q4_k_m')\n",
     "import os\n",
     "gguf_files = [f for f in os.listdir('nous-gguf') if f.endswith('.gguf')]\n",
     "print('GGUF files:', gguf_files)\n")

code("cell-download",
     "# Cell 8 — Download\n",
     "import os, shutil\n",
     "gguf_file = [f for f in os.listdir('nous-gguf') if f.endswith('.gguf')][0]\n",
     "src = f'nous-gguf/{gguf_file}'\n",
     "size_gb = os.path.getsize(src) / 1e9\n",
     "print(f'File: {src}  ({size_gb:.2f} GB)')\n",
     "\n",
     "# Option A: Direct download (< 2GB connections)\n",
     "# from google.colab import files; files.download(src)\n",
     "\n",
     "# Option B: Copy to Google Drive (recommended)\n",
     "# from google.colab import drive\n",
     "# drive.mount('/content/drive')\n",
     "# shutil.copy(src, f'/content/drive/MyDrive/nous/{gguf_file}')\n",
     "\n",
     "print('Place downloaded file at: nous/nous-3b-q4.gguf')\n")

md("md-next",
   "## After downloading\n\n",
   "1. Place the `.gguf` at `nous/nous-3b-q4.gguf`\n",
   "2. Next step: wire `llama-cpp-python` loader into `backend/main.py`\n",
   "3. Re-run `gen_dataset.py` after any taxonomy changes\n")

nb = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10.0"},
        "accelerator": "GPU",
        "colab": {"gpuType": "T4", "provenance": []}
    },
    "cells": cells,
}

out = "nous/nous_finetune.ipynb"
with open(out, "w", encoding="utf-8") as f:
    json.dump(nb, f, indent=1)
print(f"Written: {out}")
