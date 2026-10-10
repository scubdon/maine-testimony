"""Detect form-letter campaigns: near-identical testimony submitted on the same bill.

Organized campaigns often hand supporters a template ("I oppose LD 754 and LD 1519,
which..."). The online submission form also prepends each person's name and town,
so we strip the submitter's own name/town tokens before comparing.

Method: per bill, normalize text -> pairwise ``rapidfuzz.fuzz.ratio`` (vectorized
``cdist``) -> connected components above a similarity threshold. Cheap enough for
the largest hearings (~2k documents) without MinHash/LSH.
"""

from __future__ import annotations

import re
from collections import defaultdict

import numpy as np
from rapidfuzz import fuzz
from rapidfuzz.process import cdist

from .db import Store

COMPARE_CHARS = 1200


def normalize(text: str, drop_tokens: set[str]) -> str:
    words = re.findall(r"[a-z]+", text.casefold())
    return " ".join(w for w in words if w not in drop_tokens)[:COMPARE_CHARS]


def _components(n: int, edges: np.ndarray) -> list[int]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return [find(i) for i in range(n)]


def run_form_letters(store: Store, threshold: float = 90.0, min_chars: int = 80) -> dict:
    rows = store.conn.execute(
        """SELECT t.id, t.session_number, t.paper_number, t.first_name, t.last_name,
                  t.organization, d.text
           FROM testimony t JOIN documents d ON d.testimony_id = t.id
           WHERE d.text IS NOT NULL AND d.text <> ''"""
    ).fetchall()
    by_bill: dict[tuple, list] = defaultdict(list)
    for r in rows:
        drop = set(
            re.findall(
                r"[a-z]+", f"{r['first_name']} {r['last_name']} {r['organization']}".casefold()
            )
        )
        norm = normalize(r["text"], drop)
        if len(norm) >= min_chars:
            by_bill[(r["session_number"], r["paper_number"])].append((r["id"], norm))

    out, groups = [], 0
    for (snum, paper), docs in by_bill.items():
        if len(docs) < 2:
            continue
        texts = [d[1] for d in docs]
        sims = cdist(
            texts, texts, scorer=fuzz.ratio, score_cutoff=threshold, dtype=np.uint8, workers=-1
        )
        np.fill_diagonal(sims, 0)
        edges = np.argwhere(sims > 0)
        if not len(edges):
            continue
        comp = _components(len(docs), edges)
        sizes: dict[int, int] = defaultdict(int)
        for c in comp:
            sizes[c] += 1
        for i, c in enumerate(comp):
            if sizes[c] > 1:
                out.append((docs[i][0], f"{snum}-{paper}-{docs[c][0]}", sizes[c]))
        groups += sum(1 for s in sizes.values() if s > 1)

    store.conn.execute("DELETE FROM form_letters")
    store.conn.executemany("INSERT INTO form_letters VALUES (?,?,?)", out)
    store.conn.commit()
    return {"documents_compared": len(rows), "in_form_letters": len(out), "groups": groups}
