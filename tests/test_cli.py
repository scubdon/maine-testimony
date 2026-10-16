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
