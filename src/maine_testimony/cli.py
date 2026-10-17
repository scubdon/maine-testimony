"""Command-line interface: ``mtestimony --help``."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from .client import TestimonyClient
from .crawl import crawl as run_crawl
from .db import Store
from .evaluate import (
    evaluate_labels,
    evaluate_position_labels,
    is_position_labels,
    read_labeled_ids,
    write_label_sample,
    write_position_sample,
)
from .form_letters import run_form_letters
from .llm_positions import ClaudePositions, LLMFallback
from .pipeline import export_public, fetch_documents, run_classify, run_positions

app = typer.Typer(add_completion=False, help="Maine Legislature testimony dataset pipeline.")

DB = Annotated[Path, typer.Option("--db", help="SQLite database path.")]
DEFAULT_DB = Path("data/testimony.sqlite")
DEFAULT_CACHE = Path("data/raw")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-but-readable PDFs are common


@app.command()
def crawl(
    db: DB = DEFAULT_DB,
    session: Annotated[list[int] | None, typer.Option("--session", "-s")] = None,
    committee: Annotated[list[str] | None, typer.Option("--committee", "-c")] = None,
    since: Annotated[str | None, typer.Option(help="Only hearings on/after YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option(help="Only hearings on/before YYYY-MM-DD.")] = None,
    max_hearings: Annotated[int | None, typer.Option(help="Stop after N hearings.")] = None,
    per_committee: Annotated[
        int | None, typer.Option(help="Evenly sample at most N hearings per committee.")
    ] = None,
    rate: Annotated[float, typer.Option(help="Minimum seconds between requests.")] = 1.0,
    cache_dir: Annotated[Path, typer.Option(help="Raw JSON cache dir.")] = DEFAULT_CACHE,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Don't keep raw JSON.")] = False,
) -> None:
    """Pull testimony listings into SQLite (incremental and resumable)."""

    async def go():
        cache = None if no_cache else cache_dir
        async with TestimonyClient(min_interval=rate, cache_dir=cache) as client:
            store = Store(db)
            stats = await run_crawl(
                client,
                store,
                sessions=session,
                committees=committee,
                since=date.fromisoformat(since) if since else None,
                until=date.fromisoformat(until) if until else None,
                max_hearings=max_hearings,
                per_committee=per_committee,
            )
            typer.echo(
                f"hearings fetched={stats.hearings_fetched} skipped={stats.hearings_skipped} "
                f"records={stats.records} requests={client.requests_made} "
                f"errors={len(stats.errors)}; testimony rows in db={store.count('testimony')}"
            )
            for e in stats.errors[:10]:
                typer.echo(f"  ! {e}", err=True)

    asyncio.run(go())


@app.command()
def classify(db: DB = DEFAULT_DB) -> None:
    """Classify submitters (org / government / legislator / town / ...) and cluster orgs."""
    counts = run_classify(Store(db))
    total = sum(counts.values()) or 1
    for cat, n in counts.most_common():
        typer.echo(f"{cat:<14}{n:>8}  {n / total:6.1%}")


def _parse_bill(value: str) -> tuple[int, str]:
    """``132:SP0262`` -> ``(132, "SP0262")``."""
    session, sep, paper = value.partition(":")
    if not sep or not session.strip().isdigit() or not paper.strip():
        raise typer.BadParameter(f"expected SESSION:PAPER like 132:SP0262, got {value!r}")
    return int(session), paper.strip().upper()


@app.command("fetch-docs")
def fetch_docs(
    db: DB = DEFAULT_DB,
    limit: Annotated[int | None, typer.Option(help="Max documents this run.")] = None,
    rate: Annotated[float, typer.Option(help="Minimum seconds between requests.")] = 1.0,
    save_pdfs: Annotated[Path | None, typer.Option(help="Also keep PDFs in this dir.")] = None,
    random: Annotated[bool, typer.Option(help="Pick documents in random order.")] = False,
    orgs_only: Annotated[
        bool,
        typer.Option(help="Only organizations, government and legislators (run classify first)."),
    ] = False,
    bill: Annotated[
        list[str] | None,
        typer.Option(help="Only testimony on this bill, as SESSION:PAPER (e.g. 132:SP0262)."),
    ] = None,
    session: Annotated[
        list[int] | None, typer.Option("--session", "-s", help="Only these sessions.")
    ] = None,
    min_submissions: Annotated[
        int | None, typer.Option(help="Only bills with at least N testimony records.")
    ] = None,
) -> None:
    """Download testimony PDFs and store their extracted text."""
    cats = ["organization", "government", "legislator"] if orgs_only else None
    bills = [_parse_bill(b) for b in bill] if bill else None

    async def go():
        async with TestimonyClient(min_interval=rate) as client:
            stats = await fetch_documents(
                client,
                Store(db),
                limit=limit,
                save_dir=save_pdfs,
                random=random,
                categories=cats,
                bills=bills,
                sessions=session,
                min_submissions=min_submissions,
            )
            typer.echo(dict(stats))

    asyncio.run(go())


LLM = Annotated[
    bool,
    typer.Option(
        "--llm",
        help="Send regex-unknown documents to Claude (needs ANTHROPIC_API_KEY; answers are "
        "cached in the DB, and without a key only cached answers are used).",
    ),
]
LLM_LIMIT = Annotated[int | None, typer.Option(help="With --llm: at most N API calls this run.")]


def _llm_fallback(store: Store, enabled: bool, limit: int | None) -> LLMFallback | None:
    return LLMFallback(store, ClaudePositions.from_env(), limit=limit) if enabled else None


def _echo_llm_stats(fallback: LLMFallback | None) -> None:
    if fallback is not None:
        typer.echo("llm: " + ", ".join(f"{k}={v}" for k, v in sorted(fallback.stats.items())))


@app.command()
def positions(db: DB = DEFAULT_DB, llm: LLM = False, llm_limit: LLM_LIMIT = None) -> None:
    """Label each downloaded document support / oppose / neither / unknown."""
    store = Store(db)
    fallback = _llm_fallback(store, llm, llm_limit)
    counts = run_positions(store, llm=fallback)
    total = sum(counts.values()) or 1
    for pos, n in counts.most_common():
        typer.echo(f"{pos:<10}{n:>8}  {n / total:6.1%}")
    _echo_llm_stats(fallback)


@app.command("form-letters")
def form_letters(
    db: DB = DEFAULT_DB,
    threshold: Annotated[float, typer.Option(help="Similarity 0-100 to link two texts.")] = 90.0,
) -> None:
    """Group near-identical testimony on the same bill (form-letter campaigns)."""
    typer.echo(run_form_letters(Store(db), threshold=threshold))


@app.command()
def export(
    db: DB = DEFAULT_DB,
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("data/public"),
) -> None:
    """Write the public, privacy-reduced dataset (Parquet + CSV summaries)."""
    typer.echo(export_public(Store(db), out))


class LabelKind(StrEnum):
    SUBMITTERS = "submitters"
    POSITIONS = "positions"


@app.command("label-sample")
def label_sample(
    db: DB = DEFAULT_DB,
    kind: Annotated[
        LabelKind, typer.Option(help="submitters (org field) or positions (documents).")
    ] = LabelKind.SUBMITTERS,
    n: Annotated[int | None, typer.Option(help="Rows to sample [300 / 200].")] = None,
    out: Annotated[Path | None, typer.Option(help="[default: data/labels/<kind>_todo.csv]")] = None,
    seed: Annotated[
        int, typer.Option(help="Sampling seed; use a new one for each fresh evaluation.")
    ] = 13,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing --out.")] = False,
    exclude: Annotated[
        list[Path] | None,
        typer.Option(
            "--exclude",
            exists=True,
            dir_okay=False,
            help="Earlier label CSV whose testimony_ids to skip (repeatable).",
        ),
    ] = None,
) -> None:
    """Write a random sample of submitter values or documents for hand-labeling."""
    out = out or Path(f"data/labels/{kind.value}_todo.csv")
    if out.exists() and not force:
        typer.echo(f"{out} exists (it may hold labels); pass --force or a new --out", err=True)
        raise typer.Exit(1)
    try:
        skip = read_labeled_ids(exclude or [])
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    if kind is LabelKind.POSITIONS:
        written = write_position_sample(Store(db), out, n or 200, seed=seed, exclude=skip)
    else:
        written = write_label_sample(Store(db), out, n or 300, seed=seed, exclude=skip)
    note = f" (skipped {len(skip)} already-labeled ids)" if skip else ""
    typer.echo(f"wrote {written} rows to {out}{note}")


@app.command()
def evaluate(
    labels: Path,
    db: Annotated[
        Path, typer.Option("--db", help="SQLite database path (position labels only).")
    ] = DEFAULT_DB,
    llm: LLM = False,
) -> None:
    """Score the current classifier (or position regexes) against a hand-labeled CSV."""
    fallback = None
    if is_position_labels(labels):
        store = Store(db)
        fallback = _llm_fallback(store, llm, None)
        r = evaluate_position_labels(labels, store, llm=fallback)
    else:
        r = evaluate_labels(labels)
    typer.echo(f"n={r['n']}  accuracy={r['accuracy']:.1%}")
    if "committed" in r:
        typer.echo(
            f"  committed={r['committed']}  precision when committed="
            f"{r['committed_precision']:.1%}  missed {r['missed']} of {r['positioned']} "
            f"positioned letters"
        )
        typer.echo("  methods: " + ", ".join(f"{k}={v}" for k, v in r["methods"].most_common()))
    for cat, m in r["per_class"].items():
        typer.echo(f"  {cat:<13} P={m['precision']:.2f}  R={m['recall']:.2f}  n={m['support']}")
    for pair, k, ex in r["top_confusions"]:
        typer.echo(f"  {pair:<28} x{k}  e.g. {ex}")
    _echo_llm_stats(fallback)


@app.command()
def stats(db: DB = DEFAULT_DB) -> None:
    """Print row counts per table."""
    store = Store(db)
    for t in (
        "sessions",
        "committees",
        "hearings",
        "testimony",
        "documents",
        "submitters",
        "positions",
        "llm_positions",
        "form_letters",
    ):
        typer.echo(f"{t:<12}{store.count(t):>9}")


if __name__ == "__main__":
    app()
