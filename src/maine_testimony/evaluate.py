"""Hand-labeling workflow to measure classifier accuracy.

1. ``mtestimony label-sample --n 300`` writes ``data/labels/submitters_todo.csv``
   with the raw value, the predicted category, and an empty ``label`` column.
2. Fill in ``label`` (use the Category values; leave blank to skip a row).
3. ``mtestimony evaluate data/labels/submitters_todo.csv`` prints per-class
   precision / recall and the confusion pairs worth fixing.

Positions work the same way with ``label-sample --kind positions``: label each row
support / oppose / neither / unknown (unknown = the letter states no position), reading
``opening`` or the PDF at ``doc_url``. The file never shows the regex's prediction, so it
can't anchor the label; ``evaluate`` re-runs the current regexes on the stored text.

Label files contain raw free text from submitters and stay out of git (``data/``).
"""

from __future__ import annotations

import csv
import random
import re
from collections import Counter
from pathlib import Path

from .classify import Category, classify
from .client import DOC_URL
from .db import Store
from .positions import Position, classify_position

OPENING_CHARS = 400


def write_label_sample(store: Store, out: Path, n: int = 300, seed: int = 13) -> int:
    # Seeded sample in Python: SQLite's RANDOM() can't be seeded, and adding the seed to
    # a hash key in ORDER BY only rotates one fixed order (every seed gave the same rows).
    rows = store.conn.execute(
        """SELECT t.id, t.organization, s.category, s.rule
           FROM testimony t JOIN submitters s ON s.testimony_id = t.id
           ORDER BY t.id"""
    ).fetchall()
    rows = random.Random(seed).sample(rows, min(n, len(rows)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["testimony_id", "organization", "predicted", "rule", "label"])
        for r in rows:
            w.writerow([r["id"], r["organization"], r["category"], r["rule"], ""])
    return len(rows)


def write_position_sample(store: Store, out: Path, n: int = 200, seed: int = 13) -> int:
    """Sample documents with text, one per form-letter group, for position labeling.

    One row per group keeps a single mass mailing from filling the sample, so scores
    describe distinct letters; ``form_letter_size`` allows re-weighting per submission.
    """
    rows = store.conn.execute(
        """SELECT d.testimony_id, d.text, t.ld, t.paper_number, f.group_id, f.group_size
           FROM documents d JOIN testimony t ON t.id = d.testimony_id
           LEFT JOIN form_letters f ON f.testimony_id = d.testimony_id
           WHERE d.error IS NULL AND d.text <> ''
           ORDER BY d.testimony_id"""
    ).fetchall()
    seen: set[str] = set()
    distinct = []
    for r in rows:
        if r["group_id"] is not None:
            if r["group_id"] in seen:
                continue
            seen.add(r["group_id"])
        distinct.append(r)
    distinct = random.Random(seed).sample(distinct, min(n, len(distinct)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["testimony_id", "ld", "paper_number", "doc_url", "form_letter_size", "opening"]
            + ["label", "notes"]
        )
        for r in distinct:
            opening = re.sub(r"\s+", " ", r["text"]).strip()[:OPENING_CHARS]
            w.writerow(
                [
                    r["testimony_id"],
                    r["ld"],
                    r["paper_number"],
                    DOC_URL.format(id=r["testimony_id"]),
                ]
                + [r["group_size"] or 1, opening, "", ""]
            )
    return len(distinct)


def is_position_labels(path: Path) -> bool:
    with path.open() as f:
        return "doc_url" in (next(csv.reader(f), None) or [])


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
    return _score(pairs, valid)


def evaluate_position_labels(path: Path, store: Store) -> dict:
    """Re-run the *current* position regexes on the labeled documents' text and score them."""
    valid = {p.value for p in Position}
    pairs: list[tuple[str, str, str]] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip().lower()
            if label not in valid:
                continue
            doc = store.conn.execute(
                "SELECT text FROM documents WHERE testimony_id = ?", (int(row["testimony_id"]),)
            ).fetchone()
            if doc is None:
                continue
            pred = classify_position(doc["text"]).position.value
            pairs.append((label, pred, row["testimony_id"]))
    return _score(pairs, valid)


def _score(pairs: list[tuple[str, str, str]], valid: set[str]) -> dict:
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
