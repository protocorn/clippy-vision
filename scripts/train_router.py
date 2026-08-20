"""
Fine-tune a MiniLM classifier for the query router.

Model: sentence-transformers/paraphrase-MiniLM-L3-v2 (22M params)

Training strategy:
  - PRIMARY: single-label CrossEntropyLoss — train to predict the dominant category.
  - SECONDARY: at inference, any other label whose sigmoid score >= SECONDARY_THRESHOLD
    is returned as a secondary hint. Secondary labels are NOT used in the training loss —
    the imbalanced multi-hot signal (time_anchored appears as secondary in ~34% of data)
    confuses the model. The threshold approach gives secondary labels essentially for free.

This gives the best of both worlds: clean primary learning + secondary hints at runtime.

Usage:
    python scripts/train_router.py
    python scripts/train_router.py --epochs 10 --lr 2e-5
    python scripts/train_router.py --eval-only
    python scripts/train_router.py --threshold 0.35   # tune secondary threshold

Output:
    models/router_classifier/best/          checkpoint
    models/router_classifier/best/eval.txt  per-label metrics
"""

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import torch
from sklearn.metrics import classification_report
from torch import nn
from torch.utils.data import DataLoader, Dataset

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

BASE_MODEL = "sentence-transformers/paraphrase-MiniLM-L3-v2"
SEED_FILE = ROOT / "core" / "data" / "router_seed.jsonl"
GENERATED_FILE = ROOT / "core" / "data" / "router_generated.jsonl"
OUTPUT_DIR = ROOT / "models" / "router_classifier"

CATEGORIES = [
    "time_anchored",
    "topic_search",
    "specific_recall",
    "memory_query",
    "casual",
]
LABEL2ID = {c: i for i, c in enumerate(CATEGORIES)}
ID2LABEL = {i: c for c, i in LABEL2ID.items()}

TRAIN_SPLIT = 0.85
MAX_LEN = 128
BATCH_SIZE = 32
SEED = 42
SECONDARY_THRESHOLD = (
    0.20  # softmax score for a non-primary label to count as secondary hint
)


# ─────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────


def load_examples(exclude_flagged: bool = True) -> list[dict]:
    examples: list[dict] = []
    for path in [SEED_FILE, GENERATED_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
            except json.JSONDecodeError:
                continue
            if exclude_flagged and ex.get("flagged"):
                continue
            if ex.get("primary") not in LABEL2ID:
                continue
            examples.append(ex)
    return examples


def split_examples(examples: list[dict], train_ratio: float, seed: int):
    random.seed(seed)
    shuffled = examples[:]
    random.shuffle(shuffled)
    cut = int(len(shuffled) * train_ratio)
    return shuffled[:cut], shuffled[cut:]


# ─────────────────────────────────────────────────────────────
# Dataset — single-label (primary only for loss)
# ─────────────────────────────────────────────────────────────


class RouterDataset(Dataset):
    def __init__(self, examples: list[dict], tokenizer, max_len: int):
        self.examples = examples
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        ex = self.examples[idx]
        text = ex["text"].strip()
        label = LABEL2ID[ex["primary"]]  # single int for CrossEntropyLoss

        enc = self.tokenizer(
            text,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "label": torch.tensor(label, dtype=torch.long),
        }


# ─────────────────────────────────────────────────────────────
# Model
# ─────────────────────────────────────────────────────────────


class RouterClassifier(nn.Module):
    def __init__(self, base_model_name: str, num_labels: int, dropout: float = 0.1):
        super().__init__()
        from transformers import AutoModel

        self.encoder = AutoModel.from_pretrained(base_model_name)
        hidden_size = self.encoder.config.hidden_size
        self.dropout = nn.Dropout(dropout)
        self.classifier = nn.Linear(hidden_size, num_labels)

    def mean_pool(self, token_embeddings, attention_mask):
        mask = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * mask, 1) / torch.clamp(
            mask.sum(1), min=1e-9
        )

    def forward(self, input_ids, attention_mask):
        out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        pooled = self.mean_pool(out.last_hidden_state, attention_mask)
        pooled = self.dropout(pooled)
        return self.classifier(pooled)  # raw logits [batch, num_labels]


# ─────────────────────────────────────────────────────────────
# Inference helper (logits -> primary + secondary)
# ─────────────────────────────────────────────────────────────


def decode_prediction(logits: torch.Tensor, threshold: float = SECONDARY_THRESHOLD):
    """
    logits: 1-D raw scores for each label.
    Returns (primary: str, secondary: list[str])
    Primary  = argmax (always one).
    Secondary = any other label whose softmax score >= threshold.
    """
    probs = torch.softmax(logits, dim=-1)
    primary_idx = probs.argmax().item()
    primary = ID2LABEL[primary_idx]
    secondary = [
        ID2LABEL[i]
        for i, p in enumerate(probs.tolist())
        if p >= threshold and i != primary_idx
    ]
    return primary, secondary


# ─────────────────────────────────────────────────────────────
# Training — CrossEntropyLoss on primary only
# ─────────────────────────────────────────────────────────────


def compute_class_weights(examples: list[dict]) -> torch.Tensor:
    counts = Counter(ex["primary"] for ex in examples)
    total = sum(counts.values())
    weights = torch.zeros(len(CATEGORIES))
    for cat, idx in LABEL2ID.items():
        n = counts.get(cat, 1)
        weights[idx] = total / (len(CATEGORIES) * n)
    return weights


def train(args):
    from transformers import AutoTokenizer

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[train] Device: {device}")

    all_examples = load_examples(exclude_flagged=True)
    print(f"[train] Loaded {len(all_examples)} clean examples")

    from collections import Counter

    dist = Counter(ex["primary"] for ex in all_examples)
    sec_dist = Counter()
    for ex in all_examples:
        for s in ex.get("secondary", []):
            if s in LABEL2ID:
                sec_dist[s] += 1

    print("[train] Distribution (primary | as-secondary):")
    for cat in CATEGORIES:
        print(
            f"  {cat:<22} primary={dist.get(cat, 0):>4}  as-secondary={sec_dist.get(cat, 0):>4}"
        )

    train_ex, eval_ex = split_examples(all_examples, TRAIN_SPLIT, SEED)
    print(f"[train] Split: {len(train_ex)} train / {len(eval_ex)} eval")

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    train_ds = RouterDataset(train_ex, tokenizer, MAX_LEN)
    eval_ds = RouterDataset(eval_ex, tokenizer, MAX_LEN)
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True)
    eval_loader = DataLoader(eval_ds, batch_size=BATCH_SIZE)

    model = RouterClassifier(BASE_MODEL, num_labels=len(CATEGORIES)).to(device)
    cw = compute_class_weights(train_ex).to(device)
    criterion = nn.CrossEntropyLoss(weight=cw)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=args.lr,
        total_steps=len(train_loader) * args.epochs,
        pct_start=0.1,
    )

    best_acc = 0.0
    best_path = OUTPUT_DIR / "best"

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        correct = 0

        for batch in train_loader:
            input_ids = batch["input_ids"].to(device)
            attn_mask = batch["attention_mask"].to(device)
            labels = batch["label"].to(device)

            optimizer.zero_grad()
            logits = model(input_ids, attn_mask)
            loss = criterion(logits, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()

            total_loss += loss.item()
            correct += (logits.argmax(dim=1) == labels).sum().item()

        train_acc = correct / len(train_ds)
        avg_loss = total_loss / len(train_loader)
        eval_acc, report = evaluate(model, eval_loader, device, args.threshold)

        print(
            f"[epoch {epoch}/{args.epochs}]  loss={avg_loss:.4f}  train_acc={train_acc:.3f}  eval_acc={eval_acc:.3f}"
        )

        if eval_acc > best_acc:
            best_acc = eval_acc
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            save_checkpoint(model, tokenizer, best_path, report)
            print(f"  ==> New best saved  (acc={best_acc:.3f})")

    print(f"\n[done] Best eval primary accuracy: {best_acc:.3f}")
    print(f"       Checkpoint: {best_path}")


# ─────────────────────────────────────────────────────────────
# Evaluation
# ─────────────────────────────────────────────────────────────


def evaluate(model, loader, device, threshold: float = SECONDARY_THRESHOLD):
    model.eval()
    all_preds = []
    all_labels = []
    # Track how often secondary threshold fires on correct non-primary labels
    sec_fired = 0
    sec_possible = 0

    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attn_mask = batch["attention_mask"].to(device)
            labels = batch["label"]

            logits = model(input_ids, attn_mask).cpu()
            probs = torch.softmax(logits, dim=-1)
            preds = probs.argmax(dim=1)

            all_preds.extend(preds.tolist())
            all_labels.extend(labels.tolist())

            # Secondary hint stats: any non-primary label above threshold
            for i in range(len(labels)):
                for j, p in enumerate(probs[i].tolist()):
                    if j == preds[i].item():
                        continue
                    if p >= threshold:
                        sec_fired += 1
                sec_possible += 1

    acc    = sum(pred == label for pred, label in zip(all_preds, all_labels)) / len(all_labels)
    report = classification_report(
        all_labels,
        all_preds,
        target_names=CATEGORIES,
        digits=3,
        zero_division=0,
    )
    sec_rate = sec_fired / sec_possible if sec_possible else 0.0
    summary = (
        f"{report}\n"
        f"Secondary hint rate: {sec_rate:.2f}  "
        f"(fraction of examples where >= 1 non-primary label exceeded threshold={threshold})\n"
    )
    return acc, summary


# ─────────────────────────────────────────────────────────────
# Save / Load
# ─────────────────────────────────────────────────────────────


def save_checkpoint(model, tokenizer, path: Path, report: str):
    path.mkdir(parents=True, exist_ok=True)
    tokenizer.save_pretrained(str(path))
    torch.save(model.state_dict(), path / "model.pt")
    (path / "label_map.json").write_text(json.dumps(ID2LABEL, indent=2))
    (path / "config.json").write_text(
        json.dumps(
            {
                "base_model": BASE_MODEL,
                "categories": CATEGORIES,
                "secondary_threshold": SECONDARY_THRESHOLD,
            },
            indent=2,
        )
    )
    (path / "eval.txt").write_text(report)
    print(report)


def load_checkpoint(path: Path, device):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(path))
    model = RouterClassifier(BASE_MODEL, num_labels=len(CATEGORIES))
    model.load_state_dict(torch.load(path / "model.pt", map_location=device))
    model.to(device)
    model.eval()
    return model, tokenizer


# ─────────────────────────────────────────────────────────────
# Eval-only mode
# ─────────────────────────────────────────────────────────────


def eval_only(args):
    from transformers import AutoTokenizer

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt_path = OUTPUT_DIR / "best"

    if not ckpt_path.exists():
        print(f"[error] No checkpoint at {ckpt_path}. Run training first.")
        sys.exit(1)

    print(f"[eval] Loading checkpoint from {ckpt_path}")
    model, tokenizer = load_checkpoint(ckpt_path, device)

    all_examples = load_examples(exclude_flagged=True)
    _, eval_ex = split_examples(all_examples, TRAIN_SPLIT, SEED)
    print(
        f"[eval] {len(eval_ex)} held-out examples  (secondary threshold={args.threshold})"
    )

    eval_ds = RouterDataset(eval_ex, tokenizer, MAX_LEN)
    eval_loader = DataLoader(eval_ds, batch_size=BATCH_SIZE)

    acc, report = evaluate(model, eval_loader, device, args.threshold)
    print(f"Primary accuracy: {acc:.3f}\n")
    print(report)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Train the MiniLM router classifier")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument(
        "--threshold",
        type=float,
        default=SECONDARY_THRESHOLD,
        help=f"Softmax threshold for secondary label hints (default: {SECONDARY_THRESHOLD})",
    )
    parser.add_argument("--eval-only", action="store_true")
    args = parser.parse_args()

    if args.eval_only:
        eval_only(args)
    else:
        train(args)


if __name__ == "__main__":
    main()
