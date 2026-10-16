"""End-to-end over a fake crawl: classify -> positions -> form letters -> export."""

from datetime import date

import pandas as pd

from maine_testimony.crawl import crawl
from maine_testimony.form_letters import run_form_letters
from maine_testimony.models import TestimonyRecord
from maine_testimony.pipeline import export_public, run_classify, run_positions

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
