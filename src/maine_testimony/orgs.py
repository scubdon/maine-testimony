"""Organization name normalization and fuzzy clustering.

Two stages:

1. ``org_key`` – deterministic normalization (case, punctuation, legal suffixes,
   common abbreviations) plus a hand-maintained alias table for acronyms.
2. ``cluster_orgs`` – greedy fuzzy clustering of keys (most frequent first) so
   that near-duplicates such as "Maine Audubon Society" / "Maine Audubon" merge.

Acronyms are deliberately *not* auto-expanded: "MMA" is both the Maine Municipal
Association and the Maine Medical Association. Add unambiguous ones to
``data/org_aliases.csv``.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache
from importlib import resources

from rapidfuzz import fuzz, process

_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l l c|ltd|corp|corporation|co|pllc|pa|lp|llp|pc)\b\.?$"
)
_ABBREV = {
    r"\bassn\b|\bassoc\b": "association",
    r"\bdept\b": "department",
    r"\bnatl\b": "national",
    r"\bctr\b": "center",
    r"\bcomm\b": "committee",
    r"\bsvcs?\b": "services",
    r"\bme\b": "maine",
    r"\bu s\b|\busa?\b": "us",
}
_STATE_DEPT = re.compile(r"^(department|office|bureau) of ")


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    path = resources.files("maine_testimony") / "data" / "org_aliases.csv"
    with path.open() as f:
        return {row["alias"]: row["canonical"] for row in csv.DictReader(f)}


def org_key(name: str) -> str:
    """Normalize an organization name to a comparison key."""
    s = name.casefold().strip()
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^\w\s']", " ", s)
    s = s.replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the ", "", s)
    for pat, rep in _ABBREV.items():
        s = re.sub(pat, rep, s)
    for _ in range(2):
        s = _SUFFIXES.sub("", s).strip()
    s = re.sub(r" in maine$", "", s)  # "The Nature Conservancy in Maine"
    if _STATE_DEPT.match(s):
        s = "maine " + s
    return _aliases().get(s, s)


def cluster_orgs(raw_names: Iterable[str], threshold: float = 92.0) -> dict[str, tuple[str, str]]:
    """Map every raw name to ``(cluster_key, display_name)``.

    The display name is the most common raw spelling within the cluster.
    ``threshold`` is a rapidfuzz ``token_sort_ratio`` score (0-100).
    """
    raw_names = list(raw_names)
    key_of = {r: org_key(r) for r in set(raw_names)}
    key_counts = Counter(key_of[r] for r in raw_names)

    canon_of_key: dict[str, str] = {}
    canon_list: list[str] = []
    for key, _n in key_counts.most_common():
        hit = None
        if canon_list and len(key) >= 6:  # short keys (acronyms) only match exactly
            hit = process.extractOne(
                key, canon_list, scorer=fuzz.token_sort_ratio, score_cutoff=threshold
            )
        if hit:
            canon_of_key[key] = hit[0]
        else:
            canon_of_key[key] = key
            canon_list.append(key)

    # Pick a display spelling per cluster: most frequent raw string.
    spelling = Counter((canon_of_key[key_of[r]], r) for r in raw_names)
    display: dict[str, str] = {}
    for (canon, raw), _n in spelling.most_common():
        display.setdefault(canon, raw)

    return {r: (canon_of_key[key_of[r]], display[canon_of_key[key_of[r]]]) for r in key_of}
