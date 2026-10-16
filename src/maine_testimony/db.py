"""SQLite storage. One file, no server, easy to diff-check and ship to DuckDB/pandas."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

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
    error        TEXT
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

CREATE TABLE IF NOT EXISTS form_letters (
    testimony_id INTEGER PRIMARY KEY REFERENCES testimony(id),
    group_id     TEXT NOT NULL,     -- one id per cluster of near-identical texts on a bill
    group_size   INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

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
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?)",
            (testimony_id, sha256, n_bytes, n_pages, text, _now(), error),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- reads
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
    ) -> list[int]:
        """IDs still missing a document, newest first (or random).

        ``categories`` restricts to submitter categories (requires ``classify`` first).
        ``bills`` restricts to ``(session_number, paper_number)`` pairs, ``sessions`` to
        session numbers, and ``min_submissions`` to bills with at least that many testimony
        records (fetched or not). Filters combine with AND.
        """
        order = "RANDOM()" if random else "t.hearing_date DESC, t.id"
        where, params = "d.testimony_id IS NULL", []
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

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
