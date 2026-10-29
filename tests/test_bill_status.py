from datetime import date

import httpx
import pytest
from bill_pages import DIVIDED_DIED, ENACTED, NOT_A_BILL, ONTP, OPEN, bill_page
from typer.testing import CliRunner

from maine_testimony.bill_status import (
    NotABillPage,
    fetch_bill_statuses,
    parse_bill_page,
    reparse_bill_pages,
)
from maine_testimony.cli import app
from maine_testimony.client import BASE_URL, TestimonyClient
from maine_testimony.db import Store

PAGES = {101: ENACTED, 202: DIVIDED_DIED, 303: ONTP, 404: OPEN}


def test_parse_enacted():
    s = parse_bill_page(ENACTED)
    assert (s.ld, s.paper_number) == (101, "HP0101")
    assert s.final_disposition == "Emergency Enacted"
    assert s.final_date == date(2025, 3, 21)
    assert s.governor_action == "Emergency Signed"
    assert (s.law_type, s.chapter) == ("ACTPUB", 8)
    assert s.committee == "Committee on Marine Resources"
    assert s.committee_action == "Reported Out, Mar 12, 2025, OTP"
    assert s.committee_report == "Ought To Pass"
    assert s.committee_report_date == date(2025, 3, 12)
    assert s.divided_reports == () and s.divided_json() is None


def test_parse_divided_report_counts_signers():
    s = parse_bill_page(DIVIDED_DIED)
    assert s.final_disposition == "Died Between Houses"
    assert s.governor_action is None and s.chapter is None
    assert s.committee_report.startswith("MAR, MAJ: Ought To Pass As Amended")
    assert [(r.label, r.report, r.signers) for r in s.divided_reports] == [
        ("MAJ", "Ought To Pass As Amended", 8),
        ("MIN", "Ought Not To Pass", 5),
    ]
    assert '"signers": 8' in s.divided_json()


def test_parse_ontp_and_open():
    s = parse_bill_page(ONTP)
    assert s.final_disposition == "Ought Not to Pass Pursuant To Joint Rule 310"
    assert s.final_date == date(2025, 4, 11)

    s = parse_bill_page(OPEN)
    assert s.final_disposition is None and s.final_date is None
    assert s.committee_report is None
    assert s.committee_action == "Work Session Held"


def test_report_that_is_only_a_date():
    s = parse_bill_page(
        bill_page(505, "HP0505", final="Enacted, Jun 30, 2025", report="Jun 16, 2025")
    )
    assert s.committee_report is None
    assert s.committee_report_date == date(2025, 6, 16)


def test_not_a_bill_page():
    with pytest.raises(NotABillPage):
        parse_bill_page(NOT_A_BILL)


def _seed(store: Store, bills: list[tuple[int, str]]) -> None:
    now = "2025-01-01T00:00:00"
    store.conn.executemany(
        "INSERT INTO testimony (id, session_number, committee_code, hearing_date, ld,"
        " paper_number, first_seen_at, last_seen_at) VALUES (?, 132, 'MAR', '2025-02-04',"
        " ?, ?, ?, ?)",
        [(i, ld, paper, now, now) for i, (ld, paper) in enumerate(bills, 1)],
    )
    store.conn.commit()


@pytest.fixture
def bill_api():
    import respx

    with respx.mock(base_url=BASE_URL) as router:

        def page(request: httpx.Request) -> httpx.Response:
            ld = int(request.url.params["LD"])
            if ld == 999:
                return httpx.Response(200, text=NOT_A_BILL)
            return httpx.Response(200, text=PAGES[ld], headers={"content-type": "text/html"})

        yield router.get("display_ps.asp").mock(side_effect=page)


async def test_fetch_refetches_only_open_bills(bill_api, store, tmp_path):
    # Two testimony rows on LD 101 (one bill), a mislabeled paper on 303, an LD with no page.
    _seed(
        store,
        [
            (101, "HP0101"),
            (101, "HP0101"),
            (202, "SP0202"),
            (303, "SP9999"),
            (404, "HP0404"),
            (999, "HP0999"),
        ],
    )
    async with TestimonyClient(min_interval=0, cache_dir=tmp_path) as c:
        stats = await fetch_bill_statuses(c, store)
    assert stats == {"final": 3, "open": 1, "not_bill_page": 1, "paper_mismatch": 1}
    assert bill_api.call_count == 5
    row = store.conn.execute("SELECT * FROM bill_status WHERE ld = 202").fetchone()
    assert row["final_disposition"] == "Died Between Houses"
    assert row["final_date"] == "2025-06-17"
    assert row["paper_number"] == "SP0202"

    # Second run: only the open bill and the failed one go out, past the disk cache.
    assert [q[:2] for q in store.bills_needing_status()] == [(132, 404), (132, 999)]
    async with TestimonyClient(min_interval=0, cache_dir=tmp_path) as c:
        await fetch_bill_statuses(c, store)
    assert bill_api.call_count == 7


async def test_download_error_is_saved_and_retried(store, tmp_path, monkeypatch):
    import respx

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    _seed(store, [(101, "HP0101")])
    with respx.mock(base_url=BASE_URL) as router:
        router.get("display_ps.asp").mock(return_value=httpx.Response(500))
        async with TestimonyClient(min_interval=0, max_retries=0) as c:
            assert await fetch_bill_statuses(c, store) == {"download_error": 1}
    err = store.conn.execute("SELECT error FROM bill_status").fetchone()[0]
    assert err.startswith("download:")
    assert store.bills_needing_status() == [(132, 101, True)]


async def test_reparse_from_cache_needs_no_network(bill_api, tmp_path):
    db, cache = tmp_path / "t.sqlite", tmp_path / "raw"
    store = Store(db)
    _seed(store, [(101, "HP0101"), (202, "SP0202")])
    async with TestimonyClient(min_interval=0, cache_dir=cache) as c:
        await fetch_bill_statuses(c, store)
    store.conn.execute("DELETE FROM bill_status")
    store.conn.commit()
    calls = bill_api.call_count

    assert reparse_bill_pages(store, cache) == {"final": 2}
    assert store.count("bill_status") == 2 and bill_api.call_count == calls

    store.conn.execute("DELETE FROM bill_status")
    store.conn.commit()
    store.close()
    r = CliRunner().invoke(
        app, ["fetch-status", "--db", str(db), "--cache-dir", str(cache), "--reparse"]
    )
    assert r.exit_code == 0, r.output
    assert "'final': 2" in r.output
