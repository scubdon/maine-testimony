import httpx
from typer.testing import CliRunner

from maine_testimony.cli import app
from maine_testimony.db import Store


def test_label_sample_refuses_to_clobber_labels(tmp_path):
    db, out = tmp_path / "t.sqlite", tmp_path / "todo.csv"
    out.write_text("hand labels\n")
    args = ["label-sample", "--db", str(db), "--out", str(out), "--seed", "7"]

    r = CliRunner().invoke(app, args)
    assert r.exit_code == 1
    assert out.read_text() == "hand labels\n"

    r = CliRunner().invoke(app, [*args, "--force"])
    assert r.exit_code == 0, r.output
    assert out.read_text().startswith("testimony_id,organization")


def test_label_sample_seed_changes_sample(tmp_path):
    from maine_testimony.db import Store
    from maine_testimony.evaluate import write_label_sample

    store = Store(tmp_path / "t.sqlite")
    now = "2025-01-01T00:00:00"
    store.conn.executemany(
        "INSERT INTO testimony (id, session_number, committee_code, hearing_date,"
        " organization, first_seen_at, last_seen_at) VALUES (?, 132, 'HHS', '2025-02-04', ?, ?, ?)",
        [(i, f"Org {i}", now, now) for i in range(1, 2001)],
    )
    store.conn.executemany(
        "INSERT INTO submitters (testimony_id, category, rule) VALUES (?, 'organization', 'x')",
        [(i,) for i in range(1, 2001)],
    )
    store.conn.commit()

    def ids(seed: int) -> set[str]:
        out = tmp_path / f"s{seed}.csv"
        assert write_label_sample(store, out, n=100, seed=seed) == 100
        return {line.split(",")[0] for line in out.read_text().splitlines()[1:]}

    assert ids(13) == ids(13)
    assert len(ids(13) & ids(2026)) < 30  # ~5 expected by chance


def _seed_bills(db):
    store = Store(db)
    now = "2025-01-01T00:00:00"
    rows = [(1, "SP0262"), (2, "SP0262"), (3, "SP0262"), (4, "HP0101"), (5, "HP0999")]
    store.conn.executemany(
        "INSERT INTO testimony (id, session_number, committee_code, hearing_date,"
        " paper_number, first_seen_at, last_seen_at) VALUES (?, 132, 'HHS', '2025-02-04', ?, ?, ?)",
        [(i, paper, now, now) for i, paper in rows],
    )
    store.conn.commit()
    store.close()


def _fetched(db) -> list[int]:
    store = Store(db)
    try:
        return [r[0] for r in store.conn.execute("SELECT testimony_id FROM documents ORDER BY 1")]
    finally:
        store.close()


def test_fetch_docs_by_bill(tmp_path, mock_api):
    db = tmp_path / "t.sqlite"
    _seed_bills(db)
    docs = mock_api.get("getTestimonyDoc.asp").mock(return_value=httpx.Response(200, text="x"))
    args = ["fetch-docs", "--db", str(db), "--rate", "0"]
    r = CliRunner().invoke(app, [*args, "--bill", "132:hp0101", "--bill", "132:HP0999"])
    assert r.exit_code == 0, r.output
    assert _fetched(db) == [4, 5]
    assert docs.call_count == 2

    r = CliRunner().invoke(app, [*args, "--session", "132", "--min-submissions", "3"])
    assert r.exit_code == 0, r.output
    assert _fetched(db) == [1, 2, 3, 4, 5]


def test_fetch_docs_rejects_malformed_bill(tmp_path):
    r = CliRunner().invoke(
        app, ["fetch-docs", "--db", str(tmp_path / "t.sqlite"), "--bill", "SP0262"]
    )
    assert r.exit_code != 0
    assert "SESSION:PAPER" in r.output


def test_position_sample_and_evaluate(tmp_path):
    import csv

    db = tmp_path / "t.sqlite"
    store = Store(db)
    now = "2025-01-01T00:00:00"
    texts = {
        1: "I am writing in support of LD 101. Please pass it.",
        2: "I am writing in support of LD 101. Please pass it.",
        3: "I am writing in support of LD 101. Please pass it.",
        4: "I oppose LD 101 because it raises costs.",
        5: "Thank you for your time. My farm has been here for forty years.",
        6: "",  # no text layer: never sampled
    }
    store.conn.executemany(
        "INSERT INTO testimony (id, session_number, committee_code, hearing_date, ld,"
        " paper_number, first_seen_at, last_seen_at) VALUES (?, 132, 'HHS', '2025-02-04', 101,"
        " 'HP0101', ?, ?)",
        [(i, now, now) for i in texts],
    )
    store.conn.executemany(
        "INSERT INTO documents (testimony_id, text, fetched_at, error) VALUES (?, ?, ?, ?)",
        [(i, t, now, None if t else "no_text_layer") for i, t in texts.items()],
    )
    store.conn.executemany("INSERT INTO form_letters VALUES (?, 'g1', 3)", [(1,), (2,), (3,)])
    store.conn.commit()

    out = tmp_path / "positions_todo.csv"
    args = ["label-sample", "--kind", "positions", "--db", str(db), "--out", str(out)]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    rows = list(csv.DictReader(out.open()))
    assert sorted(int(x["testimony_id"]) for x in rows) == [1, 4, 5]  # group g1 once
    assert "predicted" not in rows[0]  # no anchoring on the regex's answer
    assert {x["form_letter_size"] for x in rows} == {"3", "1"}
    assert all(x["doc_url"].endswith(f"id={x['testimony_id']}") for x in rows)

    gold = {"1": "support", "4": "oppose", "5": "oppose"}  # regex misses 5
    for x in rows:
        x["label"] = gold[x["testimony_id"]]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    r = CliRunner().invoke(app, ["evaluate", str(out), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "n=3  accuracy=66.7%" in r.output
    assert "oppose -> unknown" in r.output
