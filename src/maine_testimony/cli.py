"""Command-line interface: ``mtestimony --help``."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from .client import TestimonyClient
from .crawl import crawl as run_crawl
from .db import Store
from .evaluate import evaluate_labels, write_label_sample
from .form_letters import run_form_letters
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
) -> None:
    """Download testimony PDFs and store their extracted text."""
    cats = ["organization", "government", "legislator"] if orgs_only else None

    async def go():
        async with TestimonyClient(min_interval=rate) as client:
            stats = await fetch_documents(
                client, Store(db), limit=limit, save_dir=save_pdfs, random=random, categories=cats
            )
            typer.echo(dict(stats))

    asyncio.run(go())


@app.command()
def positions(db: DB = DEFAULT_DB) -> None:
    """Label each downloaded document support / oppose / neither / unknown."""
    counts = run_positions(Store(db))
    total = sum(counts.values()) or 1
    for pos, n in counts.most_common():
        typer.echo(f"{pos:<10}{n:>8}  {n / total:6.1%}")


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


@app.command("label-sample")
def label_sample(
    db: DB = DEFAULT_DB,
    n: Annotated[int, typer.Option(help="Rows to sample.")] = 300,
    out: Annotated[Path, typer.Option()] = Path("data/labels/submitters_todo.csv"),
) -> None:
    """Write a random sample of submitter values for hand-labeling."""
    typer.echo(f"wrote {write_label_sample(Store(db), out, n)} rows to {out}")


@app.command()
def evaluate(labels: Path) -> None:
    """Score the current classifier against a hand-labeled CSV."""
    r = evaluate_labels(labels)
    typer.echo(f"n={r['n']}  accuracy={r['accuracy']:.1%}")
    for cat, m in r["per_class"].items():
        typer.echo(f"  {cat:<13} P={m['precision']:.2f}  R={m['recall']:.2f}  n={m['support']}")
    for pair, k, ex in r["top_confusions"]:
        typer.echo(f"  {pair:<28} x{k}  e.g. {ex}")


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
        "form_letters",
    ):
        typer.echo(f"{t:<12}{store.count(t):>9}")


if __name__ == "__main__":
    app()
