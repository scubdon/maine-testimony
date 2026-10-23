"""Tiny PDFs built in memory for tests (no fixtures on disk)."""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter


def blank_pdf() -> bytes:
    """A PDF with no text layer, like a scan."""
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def text_pdf(text: str) -> bytes:
    """A one-page PDF with an embedded text layer (what a searchable PDF looks like)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def encrypted_pdf(text: str) -> bytes:
    """``text_pdf`` encrypted with AES-256 and an empty user password: opens without a
    password, but pypdf needs ``cryptography`` to decrypt it."""
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(text_pdf(text))))
    w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
