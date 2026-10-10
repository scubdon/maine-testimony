"""Shared fixtures. All people in fixtures are fictional."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from maine_testimony.client import BASE_URL, TestimonyClient
from maine_testimony.db import Store

SESSIONS = [
    {"sessionNumber": 132, "sessionYears": "2025-2026"},
    {"sessionNumber": 131, "sessionYears": "2023-2024"},
]
COMMITTEES = [
    {
        "committeeName": "Marine Resources",
        "committeeCode": "MAR",
        "committeeId": 30,
        "dateCount": 2,
    },
    {"committeeName": "Taxation", "committeeCode": "TAX", "committeeId": 37, "dateCount": 1},
]
DATES = {
    "MAR": [
        {"tzo": 240, "presentedDate": "Thu Feb 6 00:00:00 EST 2025"},
        {"tzo": 240, "presentedDate": "Thu Mar 13 00:00:00 EDT 2025"},
    ],
    "TAX": [{"tzo": 240, "presentedDate": "Tue Apr 1 00:00:00 EDT 2025"}],
}


def rec(id_, first, last, org, ld=101, paper="HP0101", title="An Act to Test Things"):
    return {
        "id": id_,
        "firstName": first,
        "lastName": last,
        "organization": org,
        "ld": ld,
        "paperNumber": paper,
        "title": title,
        "sessionNumber": 132,
        "policyArea": "Marine Resources",
        "topic": "",
    }


TESTIMONY = {
    ("MAR", "2025-02-06"): [
        rec(1, "Ada", "Example", "Maine Lobstering Union"),
        rec(2, "Ben", "Sample", "Portland"),
        rec(3, "Cy", "Placeholder", "Maine State Legislature"),
        rec(4, "Dee", "Fictional", "N/A"),
    ],
    ("MAR", "2025-03-13"): [
        rec(5, "Eve", "Madeup", "Department of Marine Resources", ld=202, paper="SP0202"),
    ],
    ("TAX", "2025-04-01"): [
        rec(6, "Fay", "Invented", "topsham", ld=0, paper="IB0001"),
    ],
}


def _json(data) -> httpx.Response:
    # The real server labels JSON as text/javascript.
    return httpx.Response(200, text=json.dumps(data), headers={"content-type": "text/javascript"})


@pytest.fixture
def mock_api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("getSessionNumbersDesc.asp").mock(return_value=_json(SESSIONS))
        router.get("getTestyCmte.asp").mock(return_value=_json(COMMITTEES))

        def dates(request: httpx.Request) -> httpx.Response:
            return _json(DATES.get(request.url.params["comCode"], []))

        def testimony(request: httpx.Request) -> httpx.Response:
            p = request.url.params
            return _json(TESTIMONY.get((p["comCode"], p["hearingDate"]), []))

        router.get("getTestyCmteDates.asp").mock(side_effect=dates)
        router.get("getTestyForCmteHearingDate.asp").mock(side_effect=testimony)
        yield router


@pytest.fixture
async def client():
    c = TestimonyClient(min_interval=0.0, max_retries=2)
    yield c
    await c.aclose()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
