"""OCR for scanned testimony PDFs, the ones with no text layer (``no_text_layer``).

Two routes fill the same ``documents.text`` column, tagged in ``documents.text_source``:

* automated: ``ocrmypdf`` (pip extra ``ocr``) over Tesseract (``brew install tesseract`` /
  ``apt install tesseract-ocr``). ``fetch-docs`` uses it on new scans when it's installed,
  ``mtestimony ocr`` on scans already in the DB.
* manual: ``export-scans`` downloads the scans, an external tool (ABBYY FineReader,
  DEVONthink, ...) makes them searchable, ``import-ocr`` reads the text back.

OCR text is never written over an embedded text layer (``Store.accepts_ocr``).
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .positions import pdf_text

OCR_SOURCE = "ocrmypdf"
# ocrmypdf writes this into the sidecar for pages that already had text.
_SKIPPED = re.compile(r"^\[OCR skipped on page.*\]$", re.M)


class OCRError(RuntimeError):
    """ocrmypdf failed on a document."""


def ocrmypdf_available() -> bool:
    """ocrmypdf importable in this environment and Tesseract on PATH."""
    return bool(importlib.util.find_spec("ocrmypdf") and shutil.which("tesseract"))


def ocr_pdf(data: bytes, *, language: str = "eng", timeout: float = 600) -> str:
    """OCR a scanned PDF and return its text (empty if Tesseract found none).

    Runs ocrmypdf as a subprocess with one core: callers run several documents at once.
    Only the text sidecar is produced (``--output-type none``), never a new PDF.
    """
    with tempfile.TemporaryDirectory(prefix="mtestimony-ocr-") as tmp:
        src, sidecar = Path(tmp) / "in.pdf", Path(tmp) / "out.txt"
        src.write_bytes(data)
        cmd = [
            sys.executable, "-m", "ocrmypdf", "--quiet", "--jobs", "1",
            "--skip-text", "--rotate-pages", "--deskew",
            "--output-type", "none", "--sidecar", str(sidecar),
            "-l", language, str(src), "-",
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise OCRError(f"timed out after {timeout:.0f}s") from e
        if proc.returncode != 0:
            lines = proc.stderr.strip().splitlines()
            raise OCRError(lines[-1] if lines else f"exit {proc.returncode}")
        return clean_ocr_text(sidecar.read_text(errors="replace"))


def clean_ocr_text(text: str) -> str:
    """Normalize OCR output to look like ``pdf_text``: pages joined by newlines, no NULs."""
    return _SKIPPED.sub("", text.replace("\f", "\n")).replace("\x00", "").strip()


# ------------------------------------------------------------- manual route
def ocr_file_id(path: Path) -> int | None:
    """Testimony id from an OCR'd file's name: ``12345.pdf``, ``12345_ocr.pdf``, ``12345.txt``."""
    m = re.match(r"(\d+)", path.stem)
    return int(m.group(1)) if m else None


def read_ocr_file(path: Path) -> str:
    """Text of a searchable PDF or plain-text export from an external OCR tool."""
    if path.suffix.lower() == ".pdf":
        return clean_ocr_text(pdf_text(path.read_bytes())[0])
    raw = path.read_bytes()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return clean_ocr_text(raw.decode(encoding, errors="replace"))
