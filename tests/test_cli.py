from typer.testing import CliRunner

from maine_testimony.cli import app


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
