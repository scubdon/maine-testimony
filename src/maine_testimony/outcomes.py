"""Join testimony positions to bill outcomes: does lopsided testimony track what happens?

Unit: one bill = (session, LD). Descriptive only. Testimony is not randomly assigned
(opponents mobilize where a bill has a chance), so these are associations, not effects.

Measures, per bill:

* ``voices``: testimony with each form-letter group counted once, so a 2,000-email
  campaign is one voice. Legislators are left out: sponsors almost always testify in
  support, and that is the bill's author, not public input.
* ``oppose_share`` = oppose / (support + oppose) voices; ``neither``/``unknown`` don't count.
* ``outcome``: law | ontp | died | vetoed | withdrawn | other | open, from the final
  disposition (``outcome_of``). ``passed`` = outcome is law.
* ``report``: the committee's report, otp | ontp | divided | none (``report_of``).
"""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from .db import Store

SHARE_BINS = [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0001]
SHARE_LABELS = ["<10%", "10-25%", "25-50%", "50-75%", "75-90%", "90%+"]

# Ordered: first match wins. A chapter number means it became law whatever the wording.
_OUTCOME_RULES = [
    ("vetoed", re.compile(r"veto sustained|pocket veto", re.I)),
    ("ontp", re.compile(r"ought not to pass|\bONTP\b", re.I)),
    (
        "law",
        re.compile(r"enacted|finally passed|veto overridden|became law|law without", re.I),
    ),
    ("withdrawn", re.compile(r"withdraw", re.I)),
    ("died", re.compile(r"died|dead|indefinitely postponed|failed", re.I)),
]


def _str(x: object) -> str | None:
    """pandas reads SQL NULLs as NaN (truthy); treat anything but a non-empty str as missing."""
    return x if isinstance(x, str) and x else None


def outcome_of(final_disposition: str | None, chapter: float | int | None) -> str:
    if chapter is not None and not pd.isna(chapter):
        return "law"
    final_disposition = _str(final_disposition)
    if not final_disposition:
        return "open"
    for name, pattern in _OUTCOME_RULES:
        if pattern.search(final_disposition):
            return name
    return "other"


_ACTION_CODE = re.compile(r"Reported Out, .*?, ([A-Z/-]+)$")


def report_of(
    committee_report: str | None, divided_json: str | None, committee_action: str | None = None
) -> tuple[str, str | None]:
    """``(report, majority)``; majority is otp | ontp | tie, only for divided reports.

    Some pages give the report as a bare date; then the latest committee action's code
    ("Reported Out, Mar 3, 2025, ONTP") says what it was.
    """
    committee_report, divided_json = _str(committee_report), _str(divided_json)
    if not committee_report and not divided_json:
        m = _ACTION_CODE.search(_str(committee_action) or "")
        if not m:
            return "none", None
        code = m.group(1)
        if "/" in code:
            return "divided", None
        return ("ontp" if code == "ONTP" else "otp" if code.startswith("OTP") else "other"), None
    if committee_report and re.fullmatch(r"not reported out", committee_report, re.I):
        return "none", None
    if divided_json:
        reports = json.loads(divided_json)
        by_kind: dict[str, int] = {}
        for r in reports:
            kind = "ontp" if re.search(r"ought not", r["report"], re.I) else "otp"
            by_kind[kind] = max(by_kind.get(kind, 0), r["signers"])
        if len(by_kind) == 1:  # e.g. two OTP-AM versions: no one signed against it
            return next(iter(by_kind)), None
        otp, ontp = by_kind["otp"], by_kind["ontp"]
        return "divided", "tie" if otp == ontp else "otp" if otp > ontp else "ontp"
    if not committee_report:  # divided reports exist but no summary line
        return "none", None
    if re.search(r"ought not to pass", committee_report, re.I):
        return "ontp", None
    if re.search(r"ought to pass", committee_report, re.I):
        return "otp", None
    return "other", None


_VOICES_SQL = """
SELECT t.session_number, t.ld, MAX(t.title) AS title, MAX(t.committee_code) AS committee_code,
       COUNT(*) AS n_records,
       COUNT(DISTINCT CASE WHEN p.position = 'support'
             THEN COALESCE(f.group_id, 'r' || t.id) END) AS support,
       COUNT(DISTINCT CASE WHEN p.position = 'oppose'
             THEN COALESCE(f.group_id, 'r' || t.id) END) AS oppose,
       COUNT(DISTINCT CASE WHEN p.position = 'neither'
             THEN COALESCE(f.group_id, 'r' || t.id) END) AS neither,
       SUM(p.position = 'support') AS support_raw,
       SUM(p.position = 'oppose') AS oppose_raw,
       COUNT(DISTINCT CASE WHEN p.position = 'support' AND s.category IN {ORG}
             THEN COALESCE(s.org_key, 'r' || t.id) END) AS org_support,
       COUNT(DISTINCT CASE WHEN p.position = 'oppose' AND s.category IN {ORG}
             THEN COALESCE(s.org_key, 'r' || t.id) END) AS org_oppose
FROM testimony t
JOIN positions p ON p.testimony_id = t.id
LEFT JOIN submitters s ON s.testimony_id = t.id
LEFT JOIN form_letters f ON f.testimony_id = t.id
WHERE t.ld IS NOT NULL AND COALESCE(s.category, '') <> 'legislator'
GROUP BY 1, 2
""".replace("{ORG}", "('organization', 'government')")


def bill_table(store: Store) -> pd.DataFrame:
    """One row per bill that has both positioned testimony and a parsed status page."""
    voices = pd.read_sql(_VOICES_SQL, store.conn)
    status = pd.read_sql(
        "SELECT session_number, ld, final_disposition, chapter, committee_action,"
        " committee_report, divided_reports FROM bill_status WHERE error IS NULL",
        store.conn,
    )
    df = voices.merge(status, on=["session_number", "ld"], how="inner")
    df["outcome"] = [
        outcome_of(d, c) for d, c in zip(df.final_disposition, df.chapter, strict=True)
    ]
    df["passed"] = df.outcome.eq("law")
    reports = [
        report_of(r, d, a)
        for r, d, a in zip(
            df.committee_report, df.divided_reports, df.committee_action, strict=True
        )
    ]
    df["report"] = [r for r, _ in reports]
    df["majority"] = [m for _, m in reports]
    df["voices"] = df.support + df.oppose
    with np.errstate(invalid="ignore", divide="ignore"):
        df["oppose_share"] = df.oppose / df.voices
        df["oppose_share_raw"] = df.oppose_raw / (df.support_raw + df.oppose_raw)
    # Baseline: the pass rate of the OTHER closed bills in the same committee and session
    # (leave-one-out, so a bill never pulls its own baseline toward itself). This keeps
    # committees and sessions with unusual pass rates from driving a bin's result.
    closed = df.outcome.ne("open")
    keys = [df.session_number, df.committee_code]
    passed_sum = df.passed.where(closed, False).groupby(keys).transform("sum")
    closed_n = closed.groupby(keys).transform("sum")
    others = closed_n - closed.astype(int)
    df["cell_n"] = others
    df["cell_pass_rate"] = (passed_sum - df.passed.where(closed, False)) / others.where(others > 0)
    return df.drop(columns=["divided_reports"])


def by_opposition(
    df: pd.DataFrame, min_voices: int = 5, column: str = "oppose_share"
) -> pd.DataFrame:
    """Pass rate and committee report mix by how one-sided the testimony was."""
    d = df[(df.voices >= min_voices) & df.outcome.ne("open")].copy()
    d["bin"] = pd.cut(d[column], SHARE_BINS, labels=SHARE_LABELS, right=False)
    d["excess"] = d.passed - d.cell_pass_rate
    g = d.groupby("bin", observed=False)
    out = pd.DataFrame(
        {
            "bills": g.size(),
            "passed": g.passed.mean(),
            "vs_cell_avg": g.excess.mean(),
            "report_otp": g.report.apply(lambda r: (r == "otp").mean()),
            "report_ontp": g.report.apply(lambda r: (r == "ontp").mean()),
            "report_divided": g.report.apply(lambda r: (r == "divided").mean()),
            "died_after_otp": g.apply(
                lambda x: ((x.report == "otp") & (x.outcome == "died")).mean(),
                include_groups=False,
            ),
        }
    )
    return out


_REPORT_GROUPS = {
    ("otp", None): "OTP",
    ("divided", "otp"): "split, OTP side",
    ("divided", "tie"): "split, tie",
    ("divided", "ontp"): "split, ONTP side",
    ("ontp", None): "ONTP",
}


def by_report(df: pd.DataFrame, min_voices: int = 5, column: str = "oppose_share") -> pd.DataFrame:
    """Pass rate by opposition WITHIN each kind of committee report, as "52% (536)" cells.

    If opposition still tracks passage among bills with the same report, something after
    committee is responding to it; if not, the association runs through the committee.
    """
    d = df[(df.voices >= min_voices) & df.outcome.ne("open")].copy()
    d["bin"] = pd.cut(d[column], SHARE_BINS, labels=SHARE_LABELS, right=False)
    d["group"] = [
        _REPORT_GROUPS.get((r, m if r == "divided" else None))
        for r, m in zip(d.report, d.majority, strict=True)
    ]
    d = d[d.group.notna()]
    g = d.groupby(["bin", "group"], observed=False).passed
    out = _cells(g.mean(), g.size()).unstack("group")
    total = d.groupby("group").passed
    out.loc["all"] = _cells(total.mean(), total.size())
    return out.reindex(columns=list(_REPORT_GROUPS.values()))


def _cells(rate: pd.Series, n: pd.Series) -> pd.Series:
    return rate.map(lambda v: "-" if pd.isna(v) else f"{v:.0%}") + " (" + n.astype(str) + ")"


def by_volume(df: pd.DataFrame, min_share: float = 0.75) -> pd.DataFrame:
    """Among bills whose public testimony was mostly opposed, does more of it matter?"""
    d = df[(df.oppose_share >= min_share) & df.outcome.ne("open")].copy()
    d["size"] = pd.cut(
        d.voices, [1, 5, 20, 100, np.inf], labels=["1-4", "5-19", "20-99", "100+"], right=False
    )
    d["excess"] = d.passed - d.cell_pass_rate
    g = d.groupby("size", observed=False)
    return pd.DataFrame(
        {"bills": g.size(), "passed": g.passed.mean(), "vs_cell_avg": g.excess.mean()}
    )


def passed_despite_opposition(
    df: pd.DataFrame, min_voices: int = 20, min_share: float = 0.75, n: int = 15
) -> pd.DataFrame:
    d = df[(df.voices >= min_voices) & (df.oppose_share >= min_share) & df.passed]
    cols = ["session_number", "ld", "title", "support", "oppose", "report", "final_disposition"]
    return d.sort_values("oppose", ascending=False)[cols].head(n)
