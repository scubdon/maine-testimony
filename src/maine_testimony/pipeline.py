"""Derived-data steps that run on top of the raw crawl in SQLite."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from .classify import ORG_LIKE, Category, classify
from .client import DOC_URL, TestimonyClient, gather_limited
from .db import Store
from .llm_positions import LLMFallback
from .ocr import OCR_SOURCE, ocr_file_id, read_ocr_file
from .orgs import cluster_orgs
from .positions import Position, classify_position, pdf_text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- classify
def run_classify(store: Store) -> Counter:
    rows = store.conn.execute(
        "SELECT id, organization, first_name, last_name FROM testimony"
    ).fetchall()
    results = {r["id"]: classify(r["organization"], r["first_name"], r["last_name"]) for r in rows}
    org_raw = [r["organization"] for r in rows if results[r["id"]].category in ORG_LIKE]
    clusters = cluster_orgs(org_raw)

    out = []
    for r in rows:
        c = results[r["id"]]
        key, display = clusters.get(r["organization"], (None, None))
        if c.category not in ORG_LIKE:
            key = display = None
        out.append((r["id"], c.category.value, c.rule, key, display, c.town, c.town_geoid))

    store.conn.execute("DELETE FROM submitters")
    store.conn.executemany("INSERT INTO submitters VALUES (?,?,?,?,?,?,?)", out)
    store.conn.commit()
    return Counter(o[1] for o in out)


# --------------------------------------------------------------- documents
OCRFn = Callable[[bytes], str]  # PDF bytes -> text; ``ocr.ocr_pdf`` in production


async def _ocr_one(store: Store, tid: int, data: bytes, ocr: OCRFn, stats: Counter) -> None:
    """OCR one scan (in a thread: it's CPU-bound) and store the outcome on its row."""
    try:
        text = await asyncio.to_thread(ocr, data)
    except Exception as e:
        store.set_document_text(tid, None, source=None, error=f"ocr: {e}")
        stats["ocr_error"] += 1
        return
    if text.strip():
        store.set_document_text(tid, text, source=OCR_SOURCE)
        stats["ocr_ok"] += 1
    else:
        store.set_document_text(tid, None, source=None, error="ocr_no_text")
        stats["ocr_no_text"] += 1


async def fetch_documents(
    client: TestimonyClient,
    store: Store,
    limit: int | None = None,
    save_dir: Path | None = None,
    random: bool = False,
    categories: list[str] | None = None,
    bills: list[tuple[int, str]] | None = None,
    sessions: list[int] | None = None,
    min_submissions: int | None = None,
    ocr: OCRFn | None = None,
    retry_errors: bool = False,
) -> Counter:
    """Download and parse new PDFs; with ``ocr``, scans without a text layer get OCR'd.

    ``retry_errors`` refetches documents whose earlier download or parse failed instead
    of new ones.
    """
    ids = store.testimony_without_documents(
        limit,
        random=random,
        categories=categories,
        bills=bills,
        sessions=sessions,
        min_submissions=min_submissions,
        retry_errors=retry_errors,
    )
    stats: Counter = Counter()
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    async def one(tid: int) -> None:
        try:
            data = await client.document(tid)
        except Exception as e:
            store.save_document(tid, error=f"download: {e}")
            stats["download_error"] += 1
            return
        if save_dir:
            (save_dir / f"{tid}.pdf").write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        if not data.startswith(b"%PDF"):
            store.save_document(tid, sha256=sha, n_bytes=len(data), error="not_pdf")
            stats["not_pdf"] += 1
            return
        try:
            text, pages = pdf_text(data)
        except Exception as e:
            store.save_document(tid, sha256=sha, n_bytes=len(data), error=f"parse: {e}")
            stats["parse_error"] += 1
            return
        # Zero pages: upstream's converter kept nothing, so there is nothing to OCR.
        status = "ok" if text.strip() else "no_text_layer" if pages else "empty_pdf"
        store.save_document(
            tid,
            sha256=sha,
            n_bytes=len(data),
            n_pages=pages,
            text=text,
            error=None if status == "ok" else status,
        )
        stats[status] += 1
        if status == "no_text_layer" and ocr is not None:
            await _ocr_one(store, tid, data, ocr, stats)

    await gather_limited([one(t) for t in ids])
    return stats


async def ocr_documents(
    client: TestimonyClient, store: Store, ocr: OCRFn, limit: int | None = None
) -> Counter:
    """OCR scans already in the DB that no OCR has been tried on. PDFs aren't kept, so
    each one is downloaded again."""
    stats: Counter = Counter()

    async def one(tid: int) -> None:
        try:
            data = await client.document(tid)
        except Exception as e:
            log.warning("download %s failed: %s", tid, e)
            stats["download_error"] += 1
            return
        await _ocr_one(store, tid, data, ocr, stats)

    await gather_limited([one(t) for t in store.scanned_documents(limit, untried_only=True)])
    return stats


async def export_scans(
    client: TestimonyClient,
    store: Store,
    out_dir: Path,
    limit: int | None = None,
    include_ocr: bool = False,
) -> Counter:
    """Save scans still lacking text as ``out_dir/<testimony_id>.pdf`` for an external OCR
    tool. Files already in ``out_dir`` are kept, so an interrupted export resumes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = store.scanned_documents(include_ocr=include_ocr)
    todo = [t for t in ids if not (out_dir / f"{t}.pdf").exists()]
    stats: Counter = Counter(already_saved=len(ids) - len(todo))
    if limit:
        todo = todo[:limit]

    async def one(tid: int) -> None:
        try:
            data = await client.document(tid)
        except Exception as e:
            log.warning("download %s failed: %s", tid, e)
            stats["download_error"] += 1
            return
        (out_dir / f"{tid}.pdf").write_bytes(data)
        stats["saved"] += 1

    await gather_limited([one(t) for t in todo])
    return stats


def import_ocr(store: Store, in_dir: Path, source: str) -> Counter:
    """Store text from externally OCR'd scans: ``<testimony_id>*.txt`` or searchable
    ``<testimony_id>*.pdf`` files anywhere under ``in_dir``. Never replaces a document's
    embedded text layer. A ``.txt`` is tried before a ``.pdf`` of the same id."""
    if source == "pdf":
        raise ValueError("'pdf' is reserved for embedded text layers")
    files: dict[int, list[Path]] = {}
    stats: Counter = Counter()
    for path in sorted(in_dir.rglob("*")):
        if path.suffix.lower() not in (".pdf", ".txt") or path.name.startswith("."):
            continue
        tid = ocr_file_id(path)
        if tid is None:
            stats["no_id_in_name"] += 1
            continue
        files.setdefault(tid, []).append(path)

    for tid, paths in files.items():
        accepted = store.accepts_ocr(tid)
        if accepted is None:
            stats["not_downloaded"] += 1
            continue
        if not accepted:
            stats["has_text_layer"] += 1
            continue
        text = ""
        for path in sorted(paths, key=lambda p: p.suffix.lower() != ".txt"):
            try:
                text = read_ocr_file(path)
            except Exception as e:
                log.warning("can't read %s: %s", path, e)
            if text:
                break
        if text:
            store.set_document_text(tid, text, source=source)
            stats["imported"] += 1
        else:
            stats["no_text"] += 1
    return stats


# --------------------------------------------------------------- positions
def run_positions(store: Store, llm: LLMFallback | None = None) -> Counter:
    """Regex positions for every document; ``llm`` resolves the regex-unknown ones it can."""
    rows = store.conn.execute("SELECT testimony_id, text FROM documents").fetchall()
    results = {r["testimony_id"]: classify_position(r["text"]) for r in rows}
    if llm is not None:
        texts = {r["testimony_id"]: r["text"] for r in rows}
        results.update(
            llm.resolve(
                (tid, texts[tid])
                for tid, p in results.items()
                if p.position is Position.UNKNOWN and p.method == "regex_no_match"
            )
        )
    out = [
        (tid, p.position.value, p.method, p.evidence, p.char_offset) for tid, p in results.items()
    ]
    store.conn.execute("DELETE FROM positions")
    store.conn.executemany("INSERT INTO positions VALUES (?,?,?,?,?)", out)
    store.conn.commit()
    return Counter(o[1] for o in out)


# ------------------------------------------------------------------ export
PUBLIC_SQL = """
SELECT
  t.id                AS testimony_id,
  t.session_number,
  s.session_years,
  t.committee_code,
  c.name              AS committee_name,
  t.hearing_date,
  t.ld,
  t.paper_number,
  t.title             AS bill_title,
  t.policy_area,
  NULLIF(t.topic, '') AS topic,
  sub.category        AS submitter_category,
  sub.rule            AS classification_rule,
  sub.org_canonical   AS organization,
  sub.org_key         AS organization_key,
  sub.town,
  sub.town_geoid,
  CASE WHEN sub.category = 'legislator'
       THEN TRIM(t.first_name || ' ' || t.last_name) END AS legislator_name,
  p.position,
  p.method            AS position_method,
  COALESCE(f.group_size, 1) AS form_letter_group_size,
  f.group_id          AS form_letter_group_id
FROM testimony t
LEFT JOIN sessions   s   ON s.session_number = t.session_number
LEFT JOIN committees c   ON c.session_number = t.session_number AND c.code = t.committee_code
LEFT JOIN submitters sub ON sub.testimony_id = t.id
LEFT JOIN positions  p   ON p.testimony_id = t.id
LEFT JOIN form_letters f ON f.testimony_id = t.id
ORDER BY t.hearing_date, t.committee_code, t.paper_number, t.id
"""


def export_public(store: Store, out_dir: Path) -> dict[str, int]:
    """Write the privacy-reduced public dataset.

    Names of private individuals and the raw free-text field are never exported:
    individuals appear only as a category plus (if given) a Maine town. Legislators
    keep their names because they testify in an official capacity.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_sql_query(PUBLIC_SQL, store.conn)
    df["doc_url"] = df["testimony_id"].map(lambda i: DOC_URL.format(id=i))
    df.to_parquet(out_dir / "testimony.parquet", index=False)
    df.to_csv(out_dir / "testimony.csv", index=False)

    orgs = df[df["submitter_category"].isin(["organization", "government"])]
    org_summary = (
        orgs.groupby(["organization", "submitter_category"])
        .agg(
            submissions=("testimony_id", "count"),
            bills=("paper_number", "nunique"),
            committees=("committee_code", "nunique"),
            support=("position", lambda s: int((s == "support").sum())),
            oppose=("position", lambda s: int((s == "oppose").sum())),
            neither=("position", lambda s: int((s == "neither").sum())),
            first_seen=("hearing_date", "min"),
            last_seen=("hearing_date", "max"),
        )
        .reset_index()
        .sort_values("submissions", ascending=False)
    )
    org_summary.to_csv(out_dir / "organizations.csv", index=False)

    bills = (
        df.groupby(
            ["session_number", "paper_number", "ld", "bill_title", "committee_code"], dropna=False
        )
        .agg(
            submissions=("testimony_id", "count"),
            from_orgs=("submitter_category", lambda s: int(s.isin(["organization"]).sum())),
            from_public=(
                "submitter_category",
                lambda s: int(s.isin(["town", "place_other", "none"]).sum()),
            ),
            support=("position", lambda s: int((s == "support").sum())),
            oppose=("position", lambda s: int((s == "oppose").sum())),
            in_form_letters=("form_letter_group_size", lambda s: int((s > 1).sum())),
        )
        .reset_index()
        .sort_values("submissions", ascending=False)
    )
    bills.to_csv(out_dir / "bills.csv", index=False)

    towns = (
        df[df["submitter_category"] == Category.TOWN.value]
        .groupby(["town", "town_geoid"])
        .agg(submissions=("testimony_id", "count"), bills=("paper_number", "nunique"))
        .reset_index()
        .sort_values("submissions", ascending=False)
    )
    towns.to_csv(out_dir / "towns.csv", index=False)
    return {
        "testimony": len(df),
        "organizations": len(org_summary),
        "bills": len(bills),
        "towns": len(towns),
    }
