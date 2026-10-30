import json

import pandas as pd
import pytest

from maine_testimony.bill_status import BillStatus, DividedReport
from maine_testimony.outcomes import (
    bill_table,
    by_opposition,
    by_party,
    by_report,
    outcome_of,
    report_of,
    sponsor_majority,
)


@pytest.mark.parametrize(
    "disposition, chapter, expected",
    [
        ("Enacted", 416, "law"),
        ("Emergency Finally Passed", 9, "law"),
        ("Veto Overridden", 368, "law"),
        ("Finally Passed", None, "law"),  # e.g. a constitutional resolution (no chapter)
        ("Ought Not to Pass Pursuant To Joint Rule 310", None, "ontp"),
        ("Accepted Majority (ONTP) Report", None, "ontp"),
        ("Veto Sustained", None, "vetoed"),
        ("Pocket Veto", None, "vetoed"),
        ("Died On Adjournment", None, "died"),
        ("Died Between Houses", None, "died"),
        ("Leave to Withdraw", None, "withdrawn"),
        ("Automatic Withdrawal Pursuant to Joint Rule 305.6", None, "withdrawn"),
        ("Enactment Failed", None, "died"),
        ("Died Upon Conclusion of the 131st", None, "died"),
        ("Something New", None, "other"),
        (None, None, "open"),
        (float("nan"), float("nan"), "open"),  # how pandas reads SQL NULLs
    ],
)
def test_outcome_of(disposition, chapter, expected):
    assert outcome_of(disposition, chapter) == expected


def _divided(*reports):
    return json.dumps([{"label": lab, "report": r, "signers": n} for lab, r, n in reports])


def test_report_of():
    assert report_of("Ought To Pass As Amended", None) == ("otp", None)
    assert report_of("Ought Not To Pass", None) == ("ontp", None)
    assert report_of(None, None) == ("none", None)
    assert report_of("Not Reported Out", None) == ("none", None)
    # Some pages give only a date as the report; the action's code still says what it was.
    assert report_of(None, None, "Reported Out, Mar 3, 2025, ONTP") == ("ontp", None)
    assert report_of(None, None, "Reported Out, Jun 16, 2025, OTP-AM") == ("otp", None)
    assert report_of(None, None, "Reported Out, Jun 12, 2023, OTP-AM/ONTP") == ("divided", None)
    assert report_of(None, None, "Reported Out, May 2, 2019, LTW") == ("other", None)
    assert report_of(None, None, "Work Session Held") == ("none", None)
    maj = _divided(("MAJ", "Ought To Pass As Amended", 8), ("MIN", "Ought Not To Pass", 5))
    assert report_of("x", maj) == ("divided", "otp")
    tie = _divided(("A", "Ought To Pass As Amended", 6), ("B", "Ought Not To Pass", 6))
    assert report_of("x", tie) == ("divided", "tie")
    # Two amended versions and nobody against: the committee was for it.
    both = _divided(("A", "Ought To Pass As Amended", 9), ("B", "Ought To Pass As Amended", 4))
    assert report_of("x", both) == ("otp", None)


def _bill(store, ld, rows, status, *, committee="MAR"):
    """rows: (category, position, form-letter group or None)."""
    now = "2025-01-01T00:00:00"
    start = store.count("testimony") + 1
    for i, (cat, pos, group) in enumerate(rows, start):
        store.conn.execute(
            "INSERT INTO testimony (id, session_number, committee_code, hearing_date, ld,"
            " paper_number, title, first_seen_at, last_seen_at)"
            " VALUES (?, 132, ?, '2025-02-04', ?, ?, 'An Act', ?, ?)",
            (i, committee, ld, f"HP{ld:04d}", now, now),
        )
        store.conn.execute(
            "INSERT INTO submitters (testimony_id, category, rule, org_key) VALUES (?,?,'t',?)",
            (i, cat, f"org{i}" if cat == "organization" else None),
        )
        store.conn.execute(
            "INSERT INTO positions (testimony_id, position, method) VALUES (?,?,'regex')",
            (i, pos),
        )
        if group:
            store.conn.execute("INSERT INTO form_letters VALUES (?, ?, 0)", (i, f"{ld}-{group}"))
    store.conn.commit()
    store.save_bill_status(132, ld, status)


SPLIT = (DividedReport("MAJ", "Ought To Pass", 7), DividedReport("MIN", "Ought Not To Pass", 6))


def test_bill_table_counts_campaigns_once_and_skips_legislators(store):
    # LD 1: a 30-letter opposition campaign + 3 distinct supporters + the sponsor.
    campaign = [("town", "oppose", "g1")] * 30
    supporters = [("town", "support", None)] * 3
    sponsor = [("legislator", "support", None)]
    _bill(
        store,
        1,
        campaign + supporters + sponsor,
        BillStatus(final_disposition="Enacted", chapter=12, committee_report="Ought To Pass"),
    )
    # LD 2: four orgs opposed, unanimous ONTP.
    _bill(
        store,
        2,
        [("organization", "oppose", None)] * 4,
        BillStatus(
            final_disposition="Ought Not to Pass Pursuant To Joint Rule 310",
            committee_report="Ought Not To Pass",
        ),
    )
    # LD 3: still open; divided.
    _bill(
        store,
        3,
        [("town", "support", None)] * 2,
        BillStatus(committee_report="MAR, MAJ: ...", divided_reports=SPLIT),
    )

    df = bill_table(store).set_index("ld")
    one = df.loc[1]
    assert (one.support, one.oppose, one.oppose_raw, one.voices) == (3, 1, 30, 4)
    assert one.oppose_share == pytest.approx(0.25)
    assert one.oppose_share_raw == pytest.approx(30 / 33)
    assert one.passed and one.report == "otp"
    assert df.loc[2].org_oppose == 4 and df.loc[2].outcome == "ontp"
    assert (df.loc[3].outcome, df.loc[3].report, df.loc[3].majority) == ("open", "divided", "otp")
    # Leave-one-out baseline: LD 1's only closed neighbour (LD 2) failed, and vice versa.
    assert df.loc[1].cell_pass_rate == 0.0 and df.loc[2].cell_pass_rate == 1.0

    table = by_opposition(df.reset_index(), min_voices=2)
    assert table.loc["25-50%", "bills"] == 1 and table.loc["90%+", "bills"] == 1
    assert table.loc["90%+", "passed"] == 0.0
    assert table["bills"].sum() == 2  # the open bill is left out

    within = by_report(df.reset_index(), min_voices=2)
    assert within.loc["25-50%", "OTP"] == "100% (1)"
    assert within.loc["90%+", "ONTP"] == "0% (1)"
    assert within.loc["all", "OTP"] == "100% (1)"
    assert within.loc["<10%", "OTP"] == "- (0)"


def test_sponsor_majority():
    assert sponsor_majority(128, "SP0012", "Republican") is True  # R Senate, D House
    assert sponsor_majority(128, "HP0012", "Republican") is False
    assert sponsor_majority(131, "HP0001", "Independent") is False
    assert sponsor_majority(131, None, "Democratic") is None
    assert sponsor_majority(127, "HP0001", "Democratic") is None  # no Open States data
    assert sponsor_majority(131, "HP0001", float("nan")) is None


def test_by_party_splits_on_sponsor_standing(store):
    enacted = BillStatus(final_disposition="Enacted", chapter=1, committee_report="Ought To Pass")
    ontp = BillStatus(
        final_disposition="Ought Not to Pass Pursuant To Joint Rule 310",
        committee_report="Ought Not To Pass",
    )
    _bill(store, 1, [("town", "support", None)] * 2, enacted)  # majority sponsor, unopposed
    _bill(store, 2, [("town", "oppose", None)] * 2, ontp)  # minority sponsor, opposed
    _bill(store, 3, [("town", "oppose", None)] * 2, ontp)  # no sponsor row: left out
    store.replace_bill_sponsors(
        132,
        [
            {
                "session_number": 132,
                "ld": ld,
                "paper_number": f"HP{ld:04d}",
                "sponsor_name": "x",
                "person_id": None,
                "person_name": None,
                "party": party,
                "match": "surname",
            }
            for ld, party in ((1, "Democratic"), (2, "Republican"))
        ],
    )
    df = bill_table(store).set_index("ld")
    assert df.loc[1].sponsor_majority is True and df.loc[2].sponsor_majority is False
    assert pd.isna(df.loc[3].sponsor_majority)
    t = by_party(df.reset_index(), min_voices=2)
    assert t.loc["<10%", "passed: majority"] == "100% (1)"
    assert t.loc["90%+", "ONTP report: minority"] == "100% (1)"
    assert t.loc["all", "passed: minority"] == "0% (1)"
