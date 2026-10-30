"""SQLite storage. One file, no server, easy to diff-check and ship to DuckDB/pandas."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from .bill_status import BillStatus
from .models import Committee, Session, TestimonyRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_number INTEGER PRIMARY KEY,
    session_years  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS committees (
    session_number INTEGER NOT NULL,
    code           TEXT NOT NULL,
    committee_id   INTEGER,
    name           TEXT NOT NULL,
    date_count     INTEGER,
    PRIMARY KEY (session_number, code)
);

-- One row per (committee, hearing date) we have pulled; lets crawls resume
-- and lets us re-check only recent hearings, where late uploads still land.
CREATE TABLE IF NOT EXISTS hearings (
    session_number INTEGER NOT NULL,
    committee_code TEXT NOT NULL,
    hearing_date   TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    n_records      INTEGER NOT NULL,
    PRIMARY KEY (session_number, committee_code, hearing_date)
);

CREATE TABLE IF NOT EXISTS testimony (
    id             INTEGER PRIMARY KEY,   -- upstream id; also the PDF id
    session_number INTEGER NOT NULL,
    committee_code TEXT NOT NULL,
    hearing_date   TEXT NOT NULL,
    ld             INTEGER,
    paper_number   TEXT,
    title          TEXT,
    policy_area    TEXT,
    topic          TEXT,
    first_name     TEXT,
    last_name      TEXT,
    organization   TEXT,                  -- raw free-text field as submitted
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_testimony_bill ON testimony(session_number, paper_number);
CREATE INDEX IF NOT EXISTS ix_testimony_hearing ON testimony(committee_code, hearing_date);

CREATE TABLE IF NOT EXISTS documents (
    testimony_id INTEGER PRIMARY KEY REFERENCES testimony(id),
    sha256       TEXT,
    n_bytes      INTEGER,
    n_pages      INTEGER,
    text         TEXT,
    fetched_at   TEXT NOT NULL,
    error        TEXT,            -- e.g. no_text_layer (a scan), empty_pdf (0 pages), parse: ...
    text_source  TEXT             -- pdf (embedded layer) | ocrmypdf | an import-ocr --source
);

-- One row per bill (session, LD) that has testimony, parsed from its display_ps.asp
-- status page (`mtestimony fetch-status`). Raw: refetched only while still open.
CREATE TABLE IF NOT EXISTS bill_status (
    session_number        INTEGER NOT NULL,
    ld                    INTEGER NOT NULL,
    paper_number          TEXT,   -- as the page gives it, e.g. HP0165
    final_disposition     TEXT,   -- "Enacted", "Ought Not to Pass Pursuant To Joint Rule 310"...
    final_date            TEXT,
    governor_action       TEXT,   -- "Signed", "Veto", "Unsigned", ...
    law_type              TEXT,   -- ACTPUB | ACTPRV | RESLV ... when chaptered
    chapter               INTEGER,
    committee             TEXT,
    committee_action      TEXT,   -- "Reported Out, Mar 12, 2025, OTP-AM/ONTP"
    committee_report      TEXT,   -- "Ought To Pass" | "JUD, MAJ: ...; JUD, MIN: ..."
    committee_report_date TEXT,
    divided_reports       TEXT,   -- JSON [{label, report, signers}], NULL if unanimous
    fetched_at            TEXT NOT NULL,
    error                 TEXT,   -- download: ... | not_bill_page
    PRIMARY KEY (session_number, ld)
);

-- External: Open States primary sponsor + party per bill (`mtestimony import-sponsors`).
-- Replaced per session on each import; no network, built from files in data/external/.
CREATE TABLE IF NOT EXISTS bill_sponsors (
    session_number INTEGER NOT NULL,
    ld             INTEGER NOT NULL,
    paper_number   TEXT,   -- from Open States' bill text links, e.g. HP0165
    sponsor_name   TEXT,   -- as Open States gives it ("Joseph BALDACCI"; 128th-129th: "MASON")
    person_id      TEXT,   -- ocd-person/... in openstates/people
    person_name    TEXT,
    party          TEXT,   -- Democratic | Republican | Independent, on Feb 1 of the first year
    match          TEXT NOT NULL, -- person_id | surname | surname_chamber | ambiguous | unmatched
    PRIMARY KEY (session_number, ld)
);

-- Derived tables: fully rebuilt by `mtestimony classify` / `positions`.
CREATE TABLE IF NOT EXISTS submitters (
    testimony_id   INTEGER PRIMARY KEY REFERENCES testimony(id),
    category       TEXT NOT NULL,   -- see classify.Category
    rule           TEXT NOT NULL,   -- which rule fired, for auditability
    org_key        TEXT,            -- normalized org name (orgs/government/legislators)
    org_canonical  TEXT,            -- display name of the org cluster
    town           TEXT,            -- matched Maine municipality, if any
    town_geoid     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    testimony_id INTEGER PRIMARY KEY REFERENCES testimony(id),
    position     TEXT NOT NULL,     -- support | oppose | neither | unknown
    method       TEXT NOT NULL,     -- regex | llm | manual
    evidence     TEXT,
    char_offset  INTEGER
);

-- Cache of raw LLM answers for regex-unknown documents (`positions --llm`). NOT derived:
-- never rebuilt, so reruns don't re-bill. Keyed on the exact text sent, the model and a
-- hash of the prompt; the quote check runs at apply time in llm_positions.
CREATE TABLE IF NOT EXISTS llm_positions (
    testimony_id   INTEGER NOT NULL REFERENCES testimony(id),
    text_sha256    TEXT NOT NULL,   -- sha256 of the head text sent to the model
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    position       TEXT NOT NULL,   -- support | oppose | neither | unknown | refused
    evidence       TEXT,            -- model's quote, unverified
    created_at     TEXT NOT NULL,
    refusal_category TEXT,          -- API stop_details.category when position = refused
    PRIMARY KEY (testimony_id, text_sha256, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS ix_llm_positions_text ON llm_positions(text_sha256);

CREATE TABLE IF NOT EXISTS form_letters (
    testimony_id INTEGER PRIMARY KEY REFERENCES testimony(id),
    group_id     TEXT NOT NULL,     -- one id per cluster of near-identical texts on a bill
    group_size   INTEGER NOT NULL
);
"""


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Failed fetches worth trying again (`fetch-docs --retry-errors`): network errors, 5xx,
# truncated or unreadable files, HTML instead of a PDF. Never a scan (no_text_layer,
# ocr_*), never empty_pdf (upstream serves it that way), and never a row with text.
RETRYABLE_ERROR_SQL = (
    "(d.text_source IS NULL AND d.text IS NULL"
    " AND (d.error LIKE 'download:%' OR d.error LIKE 'parse:%' OR d.error = 'not_pdf'))"
)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an existing DB up to date: add columns CREATE TABLE IF NOT EXISTS can't
        add, and relabel rows saved under older rules."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(llm_positions)")}
        if "refusal_category" not in cols:
            self.conn.execute("ALTER TABLE llm_positions ADD COLUMN refusal_category TEXT")
            self.conn.commit()
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
        if "text_source" not in cols:
            self.conn.execute("ALTER TABLE documents ADD COLUMN text_source TEXT")
            self.conn.execute(
                "UPDATE documents SET text_source = 'pdf' WHERE error IS NULL AND text <> ''"
            )
            self.conn.commit()
        # Zero-page PDFs used to be saved as scans (no_text_layer); they have no pages to OCR.
        # Check with a read first: an UPDATE, even of 0 rows, opens a write transaction, and
        # opening an up-to-date DB must not take a write lock.
        empty = "error = 'no_text_layer' AND n_pages = 0"
        if self.conn.execute(f"SELECT 1 FROM documents WHERE {empty} LIMIT 1").fetchone():
            self.conn.execute(f"UPDATE documents SET error = 'empty_pdf' WHERE {empty}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --------------------------------------------------------------- writes
    def upsert_sessions(self, sessions: Iterable[Session]) -> None:
        self.conn.executemany(
            "INSERT INTO sessions VALUES (?, ?) ON CONFLICT DO UPDATE SET "
            "session_years=excluded.session_years",
            [(s.session_number, s.session_years) for s in sessions],
        )
        self.conn.commit()

    def upsert_committees(self, session: int, committees: Iterable[Committee]) -> None:
        self.conn.executemany(
            "INSERT INTO committees VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
            "name=excluded.name, committee_id=excluded.committee_id, "
            "date_count=excluded.date_count",
            [(session, c.code, c.committee_id, c.name, c.date_count) for c in committees],
        )
        self.conn.commit()

    def save_hearing(
        self, session: int, committee: str, hearing_date: date, records: list[TestimonyRecord]
    ) -> None:
        now = _now()
        self.conn.executemany(
            """INSERT INTO testimony VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 ld=excluded.ld, paper_number=excluded.paper_number, title=excluded.title,
                 policy_area=excluded.policy_area, topic=excluded.topic,
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 organization=excluded.organization, last_seen_at=excluded.last_seen_at""",
            [
                (
                    r.id,
                    r.session_number,
                    committee,
                    hearing_date.isoformat(),
                    r.ld,
                    r.paper_number,
                    r.title,
                    r.policy_area,
                    r.topic,
                    r.first_name,
                    r.last_name,
                    r.organization,
                    now,
                    now,
                )
                for r in records
            ],
        )
        self.conn.execute(
            "INSERT INTO hearings VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "fetched_at=excluded.fetched_at, n_records=excluded.n_records",
            (session, committee, hearing_date.isoformat(), now, len(records)),
        )
        self.conn.commit()

    def save_document(
        self,
        testimony_id: int,
        *,
        sha256: str | None = None,
        n_bytes: int | None = None,
        n_pages: int | None = None,
        text: str | None = None,
        error: str | None = None,
        text_source: str | None = None,
    ) -> None:
        if text_source is None and error is None and text and text.strip():
            text_source = "pdf"
        self.conn.execute(
            """INSERT OR REPLACE INTO documents (testimony_id, sha256, n_bytes, n_pages, text,
                 fetched_at, error, text_source) VALUES (?,?,?,?,?,?,?,?)""",
            (testimony_id, sha256, n_bytes, n_pages, text, _now(), error, text_source),
        )
        self.conn.commit()

    def set_document_text(
        self, testimony_id: int, text: str | None, *, source: str | None, error: str | None = None
    ) -> None:
        """Replace a scanned document's text (OCR), keeping the upstream file's metadata."""
        self.conn.execute(
            "UPDATE documents SET text = ?, text_source = ?, error = ? WHERE testimony_id = ?",
            (text, source, error, testimony_id),
        )
        self.conn.commit()

    def save_llm_positions(
        self,
        rows: Iterable[tuple[int, str]],
        *,
        model: str,
        prompt_version: str,
        position: str,
        evidence: str | None,
        refusal_category: str | None = None,
    ) -> None:
        """Cache one answer for every ``(testimony_id, text_sha256, ...)`` that shares it."""
        now = _now()
        self.conn.executemany(
            """INSERT OR REPLACE INTO llm_positions (testimony_id, text_sha256, model,
                 prompt_version, position, evidence, created_at, refusal_category)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (tid, sha, model, prompt_version, position, evidence, now, refusal_category)
                for tid, sha in rows
            ],
        )
        self.conn.commit()

    def save_bill_status(
        self, session: int, ld: int, status: BillStatus | None, *, error: str | None = None
    ) -> None:
        s = status or BillStatus()
        self.conn.execute(
            "INSERT OR REPLACE INTO bill_status VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                ld,
                s.paper_number,
                s.final_disposition,
                _iso(s.final_date),
                s.governor_action,
                s.law_type,
                s.chapter,
                s.committee,
                s.committee_action,
                s.committee_report,
                _iso(s.committee_report_date),
                s.divided_json(),
                _now(),
                error,
            ),
        )
        self.conn.commit()

    def replace_bill_sponsors(self, session: int, rows: list[dict]) -> None:
        cols = (
            "session_number, ld, paper_number, sponsor_name, person_id, person_name, party, match"
        )
        with self.conn:
            self.conn.execute("DELETE FROM bill_sponsors WHERE session_number = ?", (session,))
            self.conn.executemany(
                f"INSERT OR REPLACE INTO bill_sponsors ({cols}) VALUES"
                " (:session_number, :ld, :paper_number, :sponsor_name, :person_id,"
                " :person_name, :party, :match)",
                rows,
            )

    # ---------------------------------------------------------------- reads
    def bills_needing_status(
        self, sessions: list[int] | None = None, limit: int | None = None, random: bool = False
    ) -> list[tuple[int, int, bool]]:
        """``(session, ld, seen_before)`` for bills with testimony whose status page is
        missing, failed, or still has no final disposition (the bill is still moving)."""
        where = "t.ld IS NOT NULL AND (b.ld IS NULL OR b.error IS NOT NULL"
        where += " OR b.final_disposition IS NULL)"
        params: list = []
        if sessions:
            where += f" AND t.session_number IN ({','.join('?' * len(sessions))})"
            params += sessions
        order = "RANDOM()" if random else "t.session_number DESC, t.ld"
        sql = (
            "SELECT DISTINCT t.session_number, t.ld, b.ld IS NOT NULL FROM testimony t"
            " LEFT JOIN bill_status b ON b.session_number = t.session_number AND b.ld = t.ld"
            f" WHERE {where} ORDER BY {order}"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(s, ld, bool(seen)) for s, ld, seen in self.conn.execute(sql, params)]

    def llm_position(
        self, testimony_id: int, text_sha256: str, model: str, prompt_version: str
    ) -> sqlite3.Row | None:
        """A cached answer for this document, else one for any document with the same text."""
        return self.conn.execute(
            """SELECT position, evidence FROM llm_positions
               WHERE text_sha256 = ? AND model = ? AND prompt_version = ?
               ORDER BY testimony_id = ? DESC LIMIT 1""",
            (text_sha256, model, prompt_version, testimony_id),
        ).fetchone()

    def hearing_fetched_at(self, session: int, committee: str, d: date) -> datetime | None:
        row = self.conn.execute(
            "SELECT fetched_at FROM hearings WHERE session_number=? AND committee_code=? "
            "AND hearing_date=?",
            (session, committee, d.isoformat()),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def testimony_without_documents(
        self,
        limit: int | None = None,
        random: bool = False,
        categories: list[str] | None = None,
        bills: list[tuple[int, str]] | None = None,
        sessions: list[int] | None = None,
        min_submissions: int | None = None,
        retry_errors: bool = False,
    ) -> list[int]:
        """IDs still missing a document, newest first (or random).

        ``retry_errors`` queues documents whose download or parse failed instead (see
        ``RETRYABLE_ERROR_SQL``), not unfetched ones. Scans and anything with text are
        never queued, so OCR text (ocrmypdf or an ``import-ocr`` tool) can't be overwritten
        by a refetch.

        ``categories`` restricts to submitter categories (requires ``classify`` first).
        ``bills`` restricts to ``(session_number, paper_number)`` pairs, ``sessions`` to
        session numbers, and ``min_submissions`` to bills with at least that many testimony
        records (fetched or not). Filters combine with AND.
        """
        order = "RANDOM()" if random else "t.hearing_date DESC, t.id"
        where = RETRYABLE_ERROR_SQL if retry_errors else "d.testimony_id IS NULL"
        params: list = []
        join = ""
        if categories:
            join = "JOIN submitters s ON s.testimony_id = t.id"
            where += f" AND s.category IN ({','.join('?' * len(categories))})"
            params += categories
        if bills:
            where += (
                " AND ("
                + " OR ".join(["(t.session_number = ? AND t.paper_number = ?)"] * len(bills))
                + ")"
            )
            params += [v for bill in bills for v in bill]
        if sessions:
            where += f" AND t.session_number IN ({','.join('?' * len(sessions))})"
            params += sessions
        if min_submissions:
            where += (
                " AND (t.session_number, t.paper_number) IN (SELECT session_number, paper_number"
                " FROM testimony GROUP BY 1, 2 HAVING COUNT(*) >= ?)"
            )
            params.append(int(min_submissions))
        sql = (
            f"SELECT t.id FROM testimony t {join} "
            f"LEFT JOIN documents d ON d.testimony_id = t.id WHERE {where} ORDER BY {order}"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r[0] for r in self.conn.execute(sql, params)]

    def scanned_documents(
        self, limit: int | None = None, *, untried_only: bool = False, include_ocr: bool = False
    ) -> list[int]:
        """IDs of downloaded PDFs that had no text layer, newest first.

        By default: every scan still without text (never OCR'd, or OCR found nothing or
        failed). ``untried_only``: only scans no OCR has been attempted on. ``include_ocr``:
        also scans that ``ocrmypdf`` already read, so a better OCR can replace that text.
        """
        where = "d.error = 'no_text_layer'"
        if not untried_only:
            where = f"({where} OR d.error = 'ocr_no_text' OR d.error LIKE 'ocr:%')"
        if include_ocr:
            where = f"({where} OR d.text_source = 'ocrmypdf')"
        sql = (
            "SELECT d.testimony_id FROM documents d JOIN testimony t ON t.id = d.testimony_id "
            f"WHERE {where} ORDER BY t.hearing_date DESC, t.id"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r[0] for r in self.conn.execute(sql)]

    def accepts_ocr(self, testimony_id: int) -> bool | None:
        """Whether OCR text may replace this document's text: never over an embedded text
        layer. ``None`` when the document hasn't been downloaded."""
        row = self.conn.execute(
            "SELECT text_source, error FROM documents WHERE testimony_id = ?", (testimony_id,)
        ).fetchone()
        if row is None:
            return None
        source, error = row
        if source is not None:
            return source != "pdf"
        return error is not None and (
            error in ("no_text_layer", "ocr_no_text") or error.startswith("ocr:")
        )

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
