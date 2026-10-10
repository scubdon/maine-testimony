"""Hand-labeling workflow to measure classifier accuracy.

1. ``mtestimony label-sample --n 300`` writes ``data/labels/submitters_todo.csv``
   with the raw value, the predicted category, and an empty ``label`` column.
2. Fill in ``label`` (use the Category values; leave blank to skip a row).
3. ``mtestimony evaluate data/labels/submitters_todo.csv`` prints per-class
   precision / recall and the confusion pairs worth fixing.

Label files contain raw free text from submitters and stay out of git (``data/``).
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .classify import Category, classify
from .db import Store


def write_label_sample(store: Store, out: Path, n: int = 300, seed: int = 13) -> int:
    # Deterministic pseudo-random order (SQLite's RANDOM() can't be seeded).
    rows = store.conn.execute(
        """SELECT t.id, t.organization, s.category, s.rule
           FROM testimony t JOIN submitters s ON s.testimony_id = t.id
           ORDER BY (t.id * 2654435761 + ?) % 4294967296 LIMIT ?""",
        (seed, n),
    ).fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["testimony_id", "organization", "predicted", "rule", "label"])
        for r in rows:
            w.writerow([r["id"], r["organization"], r["category"], r["rule"], ""])
    return len(rows)


def evaluate_labels(path: Path) -> dict:
    """Re-run the *current* classifier on labeled rows and score it."""
    valid = {c.value for c in Category}
    pairs: list[tuple[str, str, str]] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip().lower()
            if label not in valid:
                continue
            pred = classify(row["organization"]).category.value
            pairs.append((label, pred, row["organization"]))

    report: dict[str, dict[str, float]] = {}
    for cat in sorted(valid):
        tp = sum(1 for g, p, _ in pairs if g == p == cat)
        fp = sum(1 for g, p, _ in pairs if p == cat and g != cat)
        fn = sum(1 for g, p, _ in pairs if g == cat and p != cat)
        if tp + fp + fn == 0:
            continue
        report[cat] = {
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "support": tp + fn,
        }
    errors = Counter((g, p) for g, p, _ in pairs if g != p)
    examples = {k: [o for g, p, o in pairs if (g, p) == k][:5] for k, _ in errors.most_common(8)}
    return {
        "n": len(pairs),
        "accuracy": sum(g == p for g, p, _ in pairs) / len(pairs) if pairs else 0.0,
        "per_class": report,
        "top_confusions": [
            (f"{g} -> {p}", n, examples[(g, p)]) for (g, p), n in errors.most_common(8)
        ],
    }
