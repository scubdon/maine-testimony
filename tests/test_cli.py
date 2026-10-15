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
