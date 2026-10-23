"""OCR of scanned PDFs: automated (ocrmypdf) and manual (export-scans / import-ocr)."""

from __future__ import annotations

import io
import sqlite3
import subprocess

import httpx
import pytest
from pdfs import blank_pdf, text_pdf
from typer.testing import CliRunner

from maine_testimony import cli, ocr
from maine_testimony.crawl import crawl
from maine_testimony.db import Store
from maine_testimony.pipeline import (
    export_scans,
    fetch_documents,
    import_ocr,
    ocr_documents,
    run_positions,
)

SUPPORT = "Testimony in support of LD 101"


def _docs(store: Store) -> dict[int, tuple]:
    rows = store.conn.execute("SELECT testimony_id, text, error, text_source FROM documents")
    return {r[0]: tuple(r[1:]) for r in rows}


# ------------------------------------------------------------------ helpers
def test_clean_ocr_text():
    raw = "[OCR skipped on page(s) 1]\fPage two\x00 text\f"
    assert ocr.clean_ocr_text(raw) == "Page two text"


@pytest.mark.parametrize(
    ("name", "tid"),
    [("12345.pdf", 12345), ("12345_ocr.pdf", 12345), ("12345 - FineReader.txt", 12345)],
)
def test_ocr_file_id(tmp_path, name, tid):
    assert ocr.ocr_file_id(tmp_path / name) == tid


def test_ocr_file_id_needs_leading_digits(tmp_path):
    assert ocr.ocr_file_id(tmp_path / "scan 12345.pdf") is None


def test_read_ocr_file_handles_utf16_text_export(tmp_path):
    path = tmp_path / "7.txt"
    path.write_bytes("﻿Je soutiens LD 101".encode("utf-16-le"))
    assert ocr.read_ocr_file(path) == "Je soutiens LD 101"


def test_ocr_pdf_asks_ocrmypdf_for_text_only(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        sidecar = cmd[cmd.index("--sidecar") + 1]
        with open(sidecar, "w") as f:
            f.write(SUPPORT + "\f")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    assert ocr.ocr_pdf(blank_pdf(), language="eng+fra") == SUPPORT
    cmd = seen["cmd"]
    assert cmd[cmd.index("--output-type") + 1] == "none"
    assert cmd[cmd.index("-l") + 1] == "eng+fra"
    assert "--skip-text" in cmd  # never re-OCR a page that has text


def test_ocr_pdf_raises_last_stderr_line(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 6, "", "noise\nInputFileError: bad PDF\n")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    with pytest.raises(ocr.OCRError, match="InputFileError: bad PDF"):
        ocr.ocr_pdf(b"%PDF-")


# ------------------------------------------------------------ automated OCR
async def test_fetch_documents_ocrs_only_scans(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    pdfs = {"1": blank_pdf(), "2": text_pdf("I oppose LD 101.")}
    mock_api.get("getTestimonyDoc.asp").mock(
        side_effect=lambda req: httpx.Response(200, content=pdfs[req.url.params["id"]])
    )
    calls = []

    def fake_ocr(data: bytes) -> str:
        calls.append(data)
        return SUPPORT

    for tid in (3, 4, 5, 6):  # leave only ids 1 and 2 to fetch
        store.save_document(tid, text="x")
    stats = await fetch_documents(client, store, ocr=fake_ocr)

    assert stats == {"ok": 1, "no_text_layer": 1, "ocr_ok": 1}
    assert calls == [pdfs["1"]]
    docs = _docs(store)
    assert docs[1] == (SUPPORT, None, "ocrmypdf")
    assert docs[2][1:] == (None, "pdf")
    # Upstream metadata (hash, size) still describes the original scan.
    sha, n_pages = store.conn.execute(
        "SELECT sha256, n_pages FROM documents WHERE testimony_id = 1"
    ).fetchone()
    assert sha and n_pages == 1
    run_positions(store)
    position = store.conn.execute("SELECT position FROM positions WHERE testimony_id = 1")
    assert position.fetchone()[0] == "support"


async def test_fetch_documents_without_ocr_leaves_scans(mock_api, client, store):
    await crawl(client, store, sessions=[132])
    mock_api.get("getTestimonyDoc.asp").mock(return_value=httpx.Response(200, content=blank_pdf()))
    stats = await fetch_documents(client, store, limit=1)
    assert stats == {"no_text_layer": 1}
    assert [d[1:] for d in _docs(store).values()] == [("no_text_layer", None)]


async def _seed_scans(mock_api, client, store):
    """1: untried scan, 2: text layer, 3: OCR found nothing, 4: OCR failed, 5: ocrmypdf text."""
    await crawl(client, store, sessions=[132])
    store.save_document(1, n_pages=1, text="", error="no_text_layer")
    store.save_document(2, text="I oppose LD 101.")
    store.save_document(3, error="ocr_no_text")
    store.save_document(4, error="ocr: timed out after 600s")
    store.save_document(5, text="Testimony in sup0rt", text_source="ocrmypdf")


async def test_ocr_documents_tries_each_scan_once(mock_api, client, store):
    await _seed_scans(mock_api, client, store)
    docs = mock_api.get("getTestimonyDoc.asp").mock(
        return_value=httpx.Response(200, content=blank_pdf())
    )
    stats = await ocr_documents(client, store, lambda data: "")
    assert stats == {"ocr_no_text": 1}
    assert docs.call_count == 1
    assert _docs(store)[1] == (None, "ocr_no_text", None)
    # Nothing left that OCR hasn't been tried on.
    assert await ocr_documents(client, store, lambda data: SUPPORT) == {}


async def test_ocr_error_is_recorded(mock_api, client, store):
    await _seed_scans(mock_api, client, store)
    mock_api.get("getTestimonyDoc.asp").mock(return_value=httpx.Response(200, content=b"%PDF"))

    def broken(data: bytes) -> str:
        raise ocr.OCRError("boom")

    assert await ocr_documents(client, store, broken) == {"ocr_error": 1}
    assert _docs(store)[1] == (None, "ocr: boom", None)


# --------------------------------------------------------------- manual OCR
async def test_export_scans_resumes_and_can_include_ocr(mock_api, client, store, tmp_path):
    await _seed_scans(mock_api, client, store)

    def pdf(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF " + req.url.params["id"].encode())

    mock_api.get("getTestimonyDoc.asp").mock(side_effect=pdf)
    out = tmp_path / "scans"
    out.mkdir()
    (out / "3.pdf").write_bytes(b"already here")

    stats = await export_scans(client, store, out)
    assert stats == {"saved": 2, "already_saved": 1}
    assert sorted(p.name for p in out.iterdir()) == ["1.pdf", "3.pdf", "4.pdf"]
    assert (out / "3.pdf").read_bytes() == b"already here"

    stats = await export_scans(client, store, out, include_ocr=True)
    assert stats == {"saved": 1, "already_saved": 3}
    assert (out / "5.pdf").read_bytes() == b"%PDF 5"


async def test_import_ocr_never_replaces_a_text_layer(mock_api, client, store, tmp_path):
    await _seed_scans(mock_api, client, store)
    d = tmp_path / "finereader"
    (d / "sub").mkdir(parents=True)
    (d / "1.pdf").write_bytes(blank_pdf())  # the original scan, left beside the export
    (d / "1.txt").write_text(SUPPORT)
    (d / "sub" / "3_ocr.pdf").write_bytes(text_pdf("I oppose LD 101."))
    (d / "2.txt").write_text("Overwrite attempt")
    (d / "4.pdf").write_bytes(blank_pdf())  # tool found nothing either
    (d / "5.txt").write_text("Testimony in support")  # better than ocrmypdf's reading
    (d / "999.txt").write_text("not in the db")
    (d / "notes.txt").write_text("no id")

    stats = import_ocr(store, d, "finereader")
    assert stats == {
        "imported": 3,
        "has_text_layer": 1,
        "no_text": 1,
        "not_downloaded": 1,
        "no_id_in_name": 1,
    }
    docs = _docs(store)
    assert docs[1] == (SUPPORT, None, "finereader")
    assert docs[2] == ("I oppose LD 101.", None, "pdf")
    assert docs[3] == ("I oppose LD 101.", None, "finereader")
    assert docs[4] == (None, "ocr: timed out after 600s", None)
    assert docs[5] == ("Testimony in support", None, "finereader")
    assert store.scanned_documents() == [4]


def test_import_ocr_cli_rejects_pdf_source(tmp_path):
    args = ["import-ocr", str(tmp_path), "--db", str(tmp_path / "t.sqlite"), "--source", "pdf"]
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code != 0
    assert "reserved" in r.output


def test_fetch_docs_ocr_flag_requires_ocrmypdf(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ocrmypdf_available", lambda: False)
    r = CliRunner().invoke(cli.app, ["fetch-docs", "--db", str(tmp_path / "t.sqlite"), "--ocr"])
    assert r.exit_code != 0
    assert "tesseract" in r.output


def test_migration_adds_text_source(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE documents (testimony_id INTEGER PRIMARY KEY, sha256 TEXT, n_bytes INTEGER,"
        " n_pages INTEGER, text TEXT, fetched_at TEXT NOT NULL, error TEXT)"
    )
    con.executemany(
        "INSERT INTO documents (testimony_id, text, fetched_at, error) VALUES (?, ?, 'x', ?)",
        [(1, "I support LD 1.", None), (2, "", "no_text_layer"), (3, None, "parse: bad")],
    )
    con.commit()
    con.close()
    store = Store(db)
    rows = store.conn.execute("SELECT testimony_id, text_source FROM documents ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, "pdf"), (2, None), (3, None)]
    assert store.accepts_ocr(2) and not store.accepts_ocr(1) and not store.accepts_ocr(3)
    store.close()


# ---------------------------------------------------------- real ocrmypdf
@pytest.mark.skipif(not ocr.ocrmypdf_available(), reason="ocrmypdf + tesseract not installed")
def test_ocr_pdf_reads_a_real_scan():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1700, 400), 255)
    font = ImageFont.load_default(size=64)
    ImageDraw.Draw(img).text((80, 150), "Testimony in Support of LD 1234", font=font, fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=200)
    text = ocr.ocr_pdf(buf.getvalue())
    assert "Support of LD 1234" in text
