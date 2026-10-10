from datetime import date

from maine_testimony.models import HearingDate, TestimonyRecord, parse_presented_date


def test_parse_java_date_est_and_edt():
    assert parse_presented_date("Tue Feb 4 00:00:00 EST 2025") == date(2025, 2, 4)
    assert parse_presented_date("Thu Mar 13 00:00:00 EDT 2025") == date(2025, 3, 13)


def test_parse_iso_fallback():
    assert parse_presented_date("2025-06-01") == date(2025, 6, 1)


def test_hearing_date_model():
    assert HearingDate.model_validate(
        {"tzo": 240, "presentedDate": "Wed Feb 5 00:00:00 EST 2025"}
    ).presented_date == date(2025, 2, 5)


def test_record_cleans_whitespace_and_zero_ld():
    r = TestimonyRecord.model_validate(
        {
            "id": 9,
            "firstName": " Ada ",
            "lastName": "Example",
            "organization": "  Maine   Audubon ",
            "ld": 0,
            "paperNumber": "IB0001",
            "title": "t",
            "sessionNumber": 132,
            "policyArea": "x",
            "topic": None,
        }
    )
    assert r.organization == "Maine Audubon"
    assert r.first_name == "Ada"
    assert r.ld is None
    assert r.topic == ""
