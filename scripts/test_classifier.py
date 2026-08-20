"""
Interactive test for the fine-tuned MiniLM router classifier.

Usage:
    python scripts/test_classifier.py
    python scripts/test_classifier.py --threshold 0.20
    python scripts/test_classifier.py --query "what did I do yesterday?"
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import torch
from transformers import AutoTokenizer

# ── Config ──────────────────────────────────────────────────
CHECKPOINT = ROOT / "models" / "router_classifier" / "best"
BASE_MODEL = "sentence-transformers/paraphrase-MiniLM-L3-v2"
MAX_LEN = 128
CATEGORIES = [
    "time_anchored",
    "topic_search",
    "aggregation",
    "specific_recall",
    "memory_query",
    "casual",
    "follow_up_inherit",
]
LABEL2ID = {c: i for i, c in enumerate(CATEGORIES)}
ID2LABEL = {i: c for c, i in LABEL2ID.items()}

# ── Model (same architecture as train_router.py) ─────────────
from torch import nn


class RouterClassifier(nn.Module):
    def __init__(self, base_model_name: str, num_labels: int):
        super().__init__()
        from transformers import AutoModel

        self.encoder = AutoModel.from_pretrained(base_model_name)
        hidden_size = self.encoder.config.hidden_size
        self.dropout = nn.Dropout(0.1)
        self.classifier = nn.Linear(hidden_size, num_labels)

    def mean_pool(self, token_embeddings, attention_mask):
        mask = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * mask, 1) / torch.clamp(
            mask.sum(1), min=1e-9
        )

    def forward(self, input_ids, attention_mask):
        out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        pooled = self.mean_pool(out.last_hidden_state, attention_mask)
        return self.classifier(self.dropout(pooled))


def load_model(checkpoint: Path, device):
    if not checkpoint.exists():
        print(f"[error] No checkpoint at {checkpoint}")
        print("        Run: python scripts/train_router.py")
        sys.exit(1)

    tokenizer = AutoTokenizer.from_pretrained(str(checkpoint))
    model = RouterClassifier(BASE_MODEL, num_labels=len(CATEGORIES))
    model.load_state_dict(torch.load(checkpoint / "model.pt", map_location=device))
    model.to(device)
    model.eval()
    return model, tokenizer


def predict(text: str, model, tokenizer, device, threshold: float):
    enc = tokenizer(
        text,
        max_length=MAX_LEN,
        padding="max_length",
        truncation=True,
        return_tensors="pt",
    )
    input_ids = enc["input_ids"].to(device)
    attn_mask = enc["attention_mask"].to(device)

    with torch.no_grad():
        logits = model(input_ids, attn_mask).squeeze(0).cpu()

    probs = torch.softmax(logits, dim=-1).tolist()
    primary_idx = int(torch.tensor(probs).argmax())
    primary = ID2LABEL[primary_idx]
    secondary = [
        ID2LABEL[i] for i, p in enumerate(probs) if p >= threshold and i != primary_idx
    ]
    return primary, secondary, probs


def render(text: str, primary: str, secondary: list, probs: list, threshold: float):
    bar_width = 28

    print(f'\n  Query: "{text}"')
    print(f"  {'─' * 52}")
    print(f"  {'Category':<22} {'Score':>7}  {'Bar'}")
    print(f"  {'─' * 52}")

    for i, (cat, p) in enumerate(zip(CATEGORIES, probs)):
        filled = int(p * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        is_primary = cat == primary
        is_secondary = cat in secondary

        if is_primary:
            tag = " [PRIMARY]"
        elif is_secondary:
            tag = " [secondary]"
        else:
            tag = ""

        marker = ">" if is_primary else (" " if is_secondary else " ")
        print(f"  {marker} {cat:<20} {p:>6.1%}  {bar}{tag}")

    print(f"  {'─' * 52}")
    sec_str = ", ".join(secondary) if secondary else "none"
    print(f"  Primary:   {primary}")
    print(f"  Secondary: {sec_str}  (threshold={threshold})")
    print()


def run_interactive(model, tokenizer, device, threshold: float):
    print(f"\nRouter Classifier — interactive test")
    print(f"Checkpoint: {CHECKPOINT}")
    print(f"Secondary threshold: {threshold}  (adjust with --threshold)")
    print(f"Type a query and press Enter. Ctrl+C to quit.\n")

    while True:
        try:
            text = input("Query> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not text:
            continue

        primary, secondary, probs = predict(text, model, tokenizer, device, threshold)
        render(text, primary, secondary, probs, threshold)


def main():
    parser = argparse.ArgumentParser(
        description="Test the fine-tuned router classifier"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.20,
        help="Softmax score threshold for secondary labels (default: 0.20)",
    )
    parser.add_argument(
        "--query", type=str, default=None, help="Run a single query and exit"
    )
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[load] Device: {device}")
    model, tokenizer = load_model(CHECKPOINT, device)
    print(f"[load] Checkpoint loaded.\n")

    if args.query:
        primary, secondary, probs = predict(
            args.query, model, tokenizer, device, args.threshold
        )
        render(args.query, primary, secondary, probs, args.threshold)
    else:
        run_interactive(model, tokenizer, device, args.threshold)


if __name__ == "__main__":
    main()
