"""End-to-end over a fake crawl: classify -> positions -> form letters -> export."""

from datetime import date

import httpx
import pandas as pd
from pdfs import encrypted_pdf, text_pdf

from maine_testimony.crawl import crawl
from maine_testimony.form_letters import run_form_letters
from maine_testimony.models import TestimonyRecord
from maine_testimony.pipeline import (
    export_public,
    fetch_documents,
    run_classify,
    run_positions,
)

TEMPLATE = (
    "Dear Committee Members, I oppose LD 300 because it would harm small businesses in our "
    "communities and the jobs that depend on them. Please vote ought not to pass."
)


async def test_end_to_end(mock_api, client, store, tmp_path):
    await crawl(client, store, sessions=[132])
    counts = run_classify(store)
    assert counts == {"organization": 1, "town": 2, "legislator": 1, "none": 1, "government": 1}

    store.save_document(1, text="Testimony in support of LD 101.")
    store.save_document(2, text="I am writing in opposition to LD 101.")
    assert run_positions(store) == {"support": 1, "oppose": 1}

    out = tmp_path / "public"
    sizes = export_public(store, out)
    assert sizes["testimony"] == 6
    df = pd.read_parquet(out / "testimony.parquet")

    # Privacy: no first/last names or raw free text columns at all...
    assert not {"first_name", "last_name"} & set(df.columns)
    # ...legislators keep their name (official capacity), nobody else does.
    named = df[df["legislator_name"].notna()]
    assert named["submitter_category"].unique().tolist() == ["legislator"]
    assert "Ben" not in df.to_csv() and "Sample" not in df.to_csv()
    # Individuals are reduced to a town.
    portland = df[df["testimony_id"] == 2].iloc[0]
    assert portland["submitter_category"] == "town"
    assert pd.isna(portland["organization"]) and portland["town"] == "Portland"
    assert df.loc[df["testimony_id"] == 1, "position"].item() == "support"
    assert df["doc_url"].str.contains("getTestimonyDoc.asp").all()


def test_form_letters_group_near_duplicates(store):
    recs = [
        TestimonyRecord(
            id=i,
            firstName=f"Person{i}",
            lastName="Doe",
            organization=town,
            ld=300,
            paperNumber="HP0300",
            title="t",
            sessionNumber=132,
            policyArea="x",
            topic="",
        )
        for i, town in enumerate(["Bath", "Belfast", "Bangor", "Bethel"], start=10)
    ]
    store.save_hearing(132, "ENR", date(2025, 4, 1), recs)
    for r in recs[:3]:  # three copies of the template, each with its own header
        store.save_document(r.id, text=f"{r.first_name} Doe {r.organization} LD 300 {TEMPLATE}")
    store.save_document(
        13,
        text="I support this bill because my family runs a farm and the "
        "current rules make it hard to sell at the market every week.",
    )
    result = run_form_letters(store)
    assert result["groups"] == 1 and result["in_form_letters"] == 3
    sizes = dict(store.conn.execute("SELECT testimony_id, group_size FROM form_letters"))
    assert sizes == {10: 3, 11: 3, 12: 3}


async def test_orgs_only_document_queue(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    run_classify(store)
    ids = store.testimony_without_documents(categories=["organization", "government", "legislator"])
    assert sorted(ids) == [1, 3, 5]
    assert len(store.testimony_without_documents()) == 6


async def test_bill_filters_on_document_queue(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    queue = store.testimony_without_documents
    assert queue(bills=[(132, "SP0202")]) == [5]
    assert sorted(queue(bills=[(132, "SP0202"), (132, "IB0001")])) == [5, 6]
    assert queue(bills=[(131, "SP0202")]) == []
    assert queue(sessions=[131]) == []
    assert sorted(queue(sessions=[132], min_submissions=2)) == [1, 2, 3, 4]
    # The threshold counts all submissions on the bill, fetched or not.
    store.save_document(1, text="x")
    assert sorted(queue(min_submissions=4)) == [2, 3, 4]
    assert queue(min_submissions=5) == []
    run_classify(store)
    assert queue(categories=["legislator"], min_submissions=2) == [3]


# Errors as fetch_documents records them, and whether --retry-errors should refetch.
RETRY = {
    "download: [Errno 8] nodename nor servname provided, or not known": True,
    "download: 503": True,
    "parse: Stream has ended unexpectedly": True,
    "parse: cryptography>=3.1 is required for AES algorithm": True,
    "not_pdf": True,
    "no_text_layer": False,  # a scan: OCR's job, not a refetch's
    "ocr_no_text": False,
    "ocr: tesseract crashed": False,
}


async def test_retry_errors_queue(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    queue = store.testimony_without_documents
    for error, retried in RETRY.items():
        store.save_document(1, error=error)
        assert 1 not in queue(), error
        assert (1 in queue(retry_errors=True)) is retried, error
    # Rows with text are never refetched, whatever their source or error.
    store.save_document(1, text="Testimony in support of LD 101.")
    store.save_document(2, text="I oppose LD 101.", text_source="finereader")
    store.save_document(3, text="I oppose LD 101.", error="parse: odd", text_source="ocrmypdf")
    # Only failed rows, not unfetched ones (4, 5, 6 have no row): a retry run stays small.
    assert queue(retry_errors=True) == []
    assert sorted(queue()) == [4, 5, 6]
    # Filters still apply to retried rows.
    store.save_document(4, error="download: 503")
    store.save_document(5, error="parse: Stream has ended unexpectedly")
    assert sorted(queue(retry_errors=True)) == [4, 5]
    assert queue(retry_errors=True, bills=[(132, "SP0202")]) == [5]
    assert queue(retry_errors=True, bills=[(132, "HP0101")]) == [4]


async def test_fetch_documents_retries_failed_rows_only(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    good = text_pdf("I am writing in opposition to LD 101.")
    store.save_document(1, error="download: [Errno 8] nodename nor servname provided")
    store.save_document(2, error="parse: Stream has ended unexpectedly")
    store.save_document(3, error="no_text_layer")
    store.save_document(4, text="Testimony in support of LD 101", text_source="finereader")
    store.save_document(5, text="I am writing in opposition to LD 101.")
    store.save_document(6, error="parse: cryptography>=3.1 is required for AES algorithm")
    pdfs = {
        "1": text_pdf("Testimony in support of LD 101."),
        "2": good[: len(good) // 2],  # still truncated: the new error is recorded
        "6": encrypted_pdf("Testimony in support of LD 101."),
    }
    docs = mock_api.get("getTestimonyDoc.asp").mock(
        side_effect=lambda req: httpx.Response(200, content=pdfs[req.url.params["id"]])
    )
    before = {r[0]: tuple(r) for r in store.conn.execute("SELECT * FROM documents")}

    assert await fetch_documents(client, store) == {}  # off by default: nothing queued
    assert docs.call_count == 0

    stats = await fetch_documents(client, store, retry_errors=True)
    assert stats == {"ok": 2, "parse_error": 1}
    assert sorted(c.request.url.params["id"] for c in docs.calls) == ["1", "2", "6"]
    rows = {r[0]: tuple(r) for r in store.conn.execute("SELECT * FROM documents")}
    for tid in (3, 4, 5):  # scan, imported OCR text, good text layer: untouched
        assert rows[tid] == before[tid]
    err = dict(store.conn.execute("SELECT testimony_id, error FROM documents"))
    src = dict(store.conn.execute("SELECT testimony_id, text_source FROM documents"))
    assert err[1] is None and src[1] == "pdf"
    assert err[6] is None and src[6] == "pdf"
    assert err[2] == "parse: Stream has ended unexpectedly"

    run_positions(store)
    pos = dict(store.conn.execute("SELECT testimony_id, position FROM positions"))
    assert pos[1] == pos[6] == "support"
