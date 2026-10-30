"""Build docs/data/site.json: the aggregates behind the GitHub Pages site.

Reads the local working DB (which holds private names) and writes ONLY aggregates:
counts and shares by session, category, county, town, organization and bill. No private
individual's name, no raw `organization` text, no PDF text. Repeat-testifier counts group
people by name + town inside this script and emit only bucket sizes.

    python scripts/build_site_data.py [--db data/testimony.sqlite] [--out docs/data/site.json]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

from maine_testimony import outcomes as oc
from maine_testimony.db import Store

ROOT = Path(__file__).resolve().parents[1]
PLACES = ROOT / "src/maine_testimony/data/maine_places.csv"

# Census 2020 decennial counts (P1), Maine counties. Sum = 1,362,359 (state total).
COUNTY_POP_2020 = {
    "Androscoggin": 111139, "Aroostook": 67105, "Cumberland": 303069, "Franklin": 29456,
    "Hancock": 55478, "Kennebec": 123642, "Knox": 40607, "Lincoln": 35237,
    "Oxford": 57777, "Penobscot": 152199, "Piscataquis": 16800, "Sagadahoc": 36699,
    "Somerset": 50477, "Waldo": 39607, "Washington": 31095, "York": 211972,
}  # fmt: skip
COUNTY_FIPS = {
    "Androscoggin": "23001", "Aroostook": "23003", "Cumberland": "23005", "Franklin": "23007",
    "Hancock": "23009", "Kennebec": "23011", "Knox": "23013", "Lincoln": "23015",
    "Oxford": "23017", "Penobscot": "23019", "Piscataquis": "23021", "Sagadahoc": "23023",
    "Somerset": "23025", "Waldo": "23027", "Washington": "23029", "York": "23031",
}  # fmt: skip

# Round-3 held-out hand labels (README "Measuring accuracy", 2026-10-22).
ACCURACY = {
    "submitters": {"n": 300, "accuracy": 0.953},
    "positions": [
        {"method": "Regex only", "accuracy": 0.795, "precision": 0.951, "missed": 33},
        {"method": "Regex + Claude fallback", "accuracy": 0.915, "precision": 0.947, "missed": 7},
    ],
    "positions_n": 200,
    "positions_with_stance": 196,
}

GROUP = {  # submitter category -> display group
    "town": "Individuals",
    "place_other": "Individuals",
    "none": "Unlabeled",
    "unknown": "Unlabeled",
    "organization": "Organizations",
    "government": "Government",
    "legislator": "Legislators",
}
ORDINAL = {126: "126th", 127: "127th", 128: "128th", 129: "129th", 130: "130th",
           131: "131st", 132: "132nd", 133: "133rd"}  # fmt: skip


def r(x: float, nd: int = 4) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), nd)


def load(con) -> pd.DataFrame:
    return pd.read_sql(
        """SELECT t.id, t.session_number, t.committee_code, t.hearing_date, t.ld,
                  t.paper_number, t.title,
                  LOWER(TRIM(t.first_name)) AS fn, LOWER(TRIM(t.last_name)) AS lnm,
                  s.category, s.org_key, s.org_canonical, s.town, s.town_geoid,
                  p.position, p.method, f.group_id, f.group_size
           FROM testimony t
           LEFT JOIN submitters s ON s.testimony_id = t.id
           LEFT JOIN positions p ON p.testimony_id = t.id
           LEFT JOIN form_letters f ON f.testimony_id = t.id""",
        con,
    )


def sessions_block(df: pd.DataFrame, con) -> list[dict]:
    years = dict(con.execute("SELECT session_number, session_years FROM sessions").fetchall())
    hearings = dict(
        con.execute("SELECT session_number, COUNT(*) FROM hearings GROUP BY 1").fetchall()
    )
    out = []
    for s, d in df.groupby("session_number"):
        per_bill = d.groupby("paper_number").size().sort_values(ascending=False)
        top = max(1, int(np.ceil(len(per_bill) * 0.01)))
        groups = d.category.map(GROUP).fillna("Unlabeled").value_counts()
        public = d[d.category.isin(["town", "place_other", "none", "unknown"])]
        out.append(
            {
                "session": int(s),
                "label": ORDINAL.get(int(s), str(s)),
                "years": years.get(int(s)),
                "submissions": len(d),
                "hearings": hearings.get(int(s), 0),
                "bills": int(per_bill.size),
                "median_per_bill": float(per_bill.median()),
                "top1pct_share": r(per_bill.head(top).sum() / len(d)),
                "form_letter_share_public": r(public.group_id.notna().mean()),
                "by_group": {k: int(v) for k, v in groups.items()},
            }
        )
    return out


def monthly_block(df: pd.DataFrame) -> list[dict]:
    when = pd.to_datetime(df.hearing_date)
    first_year = 2013 + (df.session_number - 126) * 2  # the 126th sat in 2013-14
    ok = when.dt.year.between(first_year, first_year + 1)  # drops ~35 upstream date typos
    m = when[ok].dt.to_period("M").value_counts().sort_index()
    full = pd.period_range(m.index.min(), m.index.max(), freq="M")
    m = m.reindex(full, fill_value=0)
    return [{"month": str(p), "n": int(v)} for p, v in m.items()]


def positions_by_group(df: pd.DataFrame) -> list[dict]:
    d = df.assign(group=df.category.map(GROUP).fillna("Unlabeled"))
    out = []
    for g in ["Individuals", "Organizations", "Government", "Legislators"]:
        x = d[d.group == g].position.value_counts()
        out.append({"group": g, **{p: int(x.get(p, 0)) for p in
                                   ("support", "oppose", "neither", "unknown")}})  # fmt: skip
    return out


def volume_block(df: pd.DataFrame) -> list[dict]:
    """Bills grouped by how much testimony they drew: what side does it take?"""
    sizes = df.groupby(["session_number", "paper_number"]).size().rename("size")
    d = df[df.category.ne("legislator")].join(sizes, on=["session_number", "paper_number"])
    bins = [1, 5, 20, 100, 500, np.inf]
    labels = ["1-4", "5-19", "20-99", "100-499", "500+"]
    d["bucket"] = pd.cut(d["size"], bins, labels=labels, right=False)
    out = []
    for b, x in d.groupby("bucket", observed=False):
        pos = x.position.value_counts()
        known = pos.get("support", 0) + pos.get("oppose", 0) + pos.get("neither", 0)
        per_bill = x.groupby(["session_number", "paper_number"]).position.agg(
            lambda p: (p == "oppose").sum() > (p == "support").sum()
        )
        out.append(
            {
                "bucket": b,
                "bills": int(per_bill.size),
                "support": r(pos.get("support", 0) / known),
                "oppose": r(pos.get("oppose", 0) / known),
                "neither": r(pos.get("neither", 0) / known),
                "mostly_opposed_bills": r(per_bill.mean()),
            }
        )
    return out


def bill_titles(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby(["session_number", "paper_number"]).agg(
        ld=("ld", "max"), title=("title", "max"), committee=("committee_code", "max")
    )


def top_bills_block(df: pd.DataFrame, status: pd.DataFrame, n: int = 20) -> list[dict]:
    g = df.groupby(["session_number", "paper_number"])
    t = bill_titles(df).assign(
        submissions=g.size(),
        support=g.position.agg(lambda p: int((p == "support").sum())),
        oppose=g.position.agg(lambda p: int((p == "oppose").sum())),
        form_letters=g.group_id.agg(lambda x: int(x.notna().sum())),
    )
    t = t.sort_values("submissions", ascending=False).head(n).reset_index()
    t = t.merge(status, how="left", left_on=["session_number", "ld"],
                right_on=["session_number", "ld"])  # fmt: skip
    return [
        {
            "session": int(x.session_number),
            "ld": None if pd.isna(x.ld) else int(x.ld),
            "title": x.title,
            "committee": x.committee,
            "submissions": int(x.submissions),
            "support": int(x.support),
            "oppose": int(x.oppose),
            "form_letters": int(x.form_letters),
            "outcome": None if pd.isna(x.outcome) else x.outcome,
        }
        for x in t.itertuples()
    ]


def campaigns_block(df: pd.DataFrame, n: int = 12) -> dict:
    d = df[df.group_id.notna()]
    titles = bill_titles(df)
    out = []
    for _gid, x in d.groupby("group_id"):
        pos = x.position.value_counts()
        key = (x.session_number.iloc[0], x.paper_number.iloc[0])
        out.append(
            {
                "size": len(x),
                "session": int(key[0]),
                "ld": None if pd.isna(titles.loc[key, "ld"]) else int(titles.loc[key, "ld"]),
                "title": titles.loc[key, "title"],
                "position": pos.index[0] if len(pos) else "unknown",
                "position_share": r(pos.iloc[0] / len(x)) if len(pos) else None,
            }
        )
    out.sort(key=lambda c: -c["size"])
    sizes = d.groupby("group_id").size()
    return {
        "groups": int(sizes.size),
        "letters": int(sizes.sum()),
        "groups_25_plus": int((sizes >= 25).sum()),
        "largest": out[:n],
    }


def orgs_block(df: pd.DataFrame, n: int = 30) -> list[dict]:
    d = df[df.category.isin(["organization", "government"]) & df.org_canonical.notna()]
    out = []
    for name, x in d.groupby("org_canonical"):
        if len(x) < 150:
            continue
        pos = x.position.value_counts()
        known = sum(pos.get(p, 0) for p in ("support", "oppose", "neither"))
        out.append(
            {
                "name": name,
                "category": x.category.mode().iloc[0],
                "submissions": len(x),
                "bills": int(x.groupby(["session_number", "paper_number"]).ngroups),
                "committees": int(x.committee_code.nunique()),
                "support": r(pos.get("support", 0) / known) if known else None,
                "oppose": r(pos.get("oppose", 0) / known) if known else None,
                "neither": r(pos.get("neither", 0) / known) if known else None,
            }
        )
    out.sort(key=lambda o: -o["submissions"])
    return out[:n]


def org_pairs_block(df: pd.DataFrame, min_positions: int = 200, min_shared: int = 25) -> dict:
    """How often two frequent organizations took the same side on the same bill."""
    d = df[
        df.category.eq("organization")
        & df.org_canonical.notna()
        & df.position.isin(["support", "oppose"])
    ]
    # One stance per org per bill (most common, ties dropped).
    stance = (
        d.groupby(["org_canonical", "session_number", "paper_number"])
        .position.agg(lambda p: p.mode().iloc[0] if p.mode().size == 1 else None)
        .dropna()
    )
    counts = stance.groupby(level=0).size()
    keep = counts[counts >= min_positions].index
    stance = stance[stance.index.get_level_values(0).isin(keep)]
    by_org = {o: s.droplevel(0) for o, s in stance.groupby(level=0)}
    pairs = []
    for a, b in combinations(sorted(by_org), 2):
        j = pd.concat([by_org[a], by_org[b]], axis=1, join="inner")
        if len(j) < min_shared:
            continue
        pairs.append(
            {"a": a, "b": b, "shared": len(j), "agree": r((j.iloc[:, 0] == j.iloc[:, 1]).mean())}
        )
    pairs.sort(key=lambda p: p["agree"])
    # Order orgs so allies sit together: leading eigenvector of the agreement matrix
    # (agree mapped to -1..1, unknown pairs 0).
    names = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    idx = {n: i for i, n in enumerate(names)}
    m = np.zeros((len(names), len(names)))
    for p in pairs:
        m[idx[p["a"]], idx[p["b"]]] = m[idx[p["b"]], idx[p["a"]]] = 2 * p["agree"] - 1
    vec = np.linalg.eigh(m)[1][:, -1]
    if vec[idx.get("Maine Policy Institute", 0)] > 0:  # keep the orientation stable
        vec = -vec
    order = [names[i] for i in np.argsort(vec)]
    return {
        "orgs": int(len(by_org)),
        "order": order,
        "pairs": len(pairs),
        "most_opposed": pairs[:12],
        "most_aligned": sorted(pairs, key=lambda p: (-p["agree"], -p["shared"]))[:12],
        "all": pairs,
    }


def places_block(df: pd.DataFrame) -> dict:
    places = pd.read_csv(PLACES, dtype={"geoid": str})
    # Census villages (CDPs) carry no county: take the county of the nearest place that has one.
    has = places[places.county.notna()]
    for i, v in places[places.county.isna()].iterrows():
        d2 = (has.lat - v.lat) ** 2 + ((has.lon - v.lon) * np.cos(np.radians(v.lat))) ** 2
        places.loc[i, "county"] = has.county.loc[d2.idxmin()]
    ind = df[df.category.eq("town") & df.town_geoid.notna()]
    by_town = ind.groupby("town_geoid").agg(
        n=("id", "size"),
        oppose=("position", lambda p: int((p == "oppose").sum())),
        support=("position", lambda p: int((p == "support").sum())),
    )
    t = places.merge(by_town, left_on="geoid", right_index=True)
    towns = [
        {
            "name": x.name,
            "county": x.county,
            "lat": r(x.lat, 3),
            "lon": r(x.lon, 3),
            "n": int(x.n),
        }
        for x in t.sort_values("n", ascending=False).itertuples()
    ]
    c = t.groupby("county")[["n", "oppose", "support"]].sum()
    counties = [
        {
            "county": k,
            "fips": COUNTY_FIPS[k],
            "n": int(v.n),
            "pop": COUNTY_POP_2020[k],
            "per_1k": r(1000 * v.n / COUNTY_POP_2020[k], 1),
            "oppose_share": r(v.oppose / (v.oppose + v.support)),
        }
        for k, v in c.iterrows()
    ]
    return {"towns": towns, "counties": counties, "towns_matched": int(len(towns))}


def repeat_block(df: pd.DataFrame) -> list[dict]:
    """People (first + last name + town) grouped by how many times they testified. Counts only."""
    ind = df[df.category.eq("town") & df.fn.fillna("").ne("") & df.lnm.fillna("").ne("")]
    per_person = ind.groupby(["fn", "lnm", "town"]).size()
    bins = [1, 2, 3, 6, 11, 26, np.inf]
    labels = ["1", "2", "3-5", "6-10", "11-25", "26+"]
    b = pd.cut(per_person, bins, labels=labels, right=False)
    people = b.value_counts().reindex(labels)
    letters = per_person.groupby(b, observed=False).sum().reindex(labels)
    return [
        {
            "times": k,
            "people": int(people[k]),
            "people_share": r(people[k] / people.sum()),
            "testimony_share": r(letters[k] / letters.sum()),
        }
        for k in labels
    ]


def committees_block(df: pd.DataFrame, con) -> list[dict]:
    names = dict(
        con.execute(
            "SELECT code, name FROM committees c WHERE session_number = "
            "(SELECT MAX(session_number) FROM committees WHERE code = c.code)"
        ).fetchall()
    )
    out = []
    for code, x in df.groupby("committee_code"):
        if len(x) < 2000:
            continue
        pos = x.position.value_counts()
        known = pos.get("support", 0) + pos.get("oppose", 0) + pos.get("neither", 0)
        out.append(
            {
                "code": code,
                "name": names.get(code, code),
                "submissions": len(x),
                "oppose": r(pos.get("oppose", 0) / known),
                "support": r(pos.get("support", 0) / known),
                "individuals": r(x.category.isin(["town", "place_other"]).mean()),
            }
        )
    out.sort(key=lambda c: -c["submissions"])
    return out


def outcomes_block(store: Store) -> dict:
    b = oc.bill_table(store)
    closed = b[b.outcome.ne("open")]
    d = closed[closed.voices >= 5].copy()
    d["bin"] = pd.cut(d.oppose_share, oc.SHARE_BINS, labels=oc.SHARE_LABELS, right=False)
    d["excess"] = d.passed - d.cell_pass_rate
    d["ontp_report"] = d.report.eq("ontp") | (d.report.eq("divided") & d.majority.eq("ontp"))

    by_opp = [
        {
            "bin": k,
            "bills": len(x),
            "passed": r(x.passed.mean()),
            "vs_cell": r(x.excess.mean()),
            "ontp_report": r(x.ontp_report.mean()),
            "report_ontp": r(x.report.eq("ontp").mean()),
        }
        for k, x in d.groupby("bin", observed=False)
    ]
    groups = {
        ("otp", None): "Unanimous OTP",
        ("divided", "otp"): "Split, OTP majority",
        ("divided", "ontp"): "Split, ONTP majority",
        ("ontp", None): "Unanimous ONTP",
    }
    d["group"] = [
        groups.get((rep, m if rep == "divided" else None))
        for rep, m in zip(d.report, d.majority, strict=True)
    ]
    by_report = [
        {"group": grp, "bin": k, "bills": len(x), "passed": r(x.passed.mean()) if len(x) else None}
        for grp in groups.values()
        for k, x in d[d.group == grp].groupby("bin", observed=False)
    ]
    p = d[d.sponsor_majority.notna()].copy()
    p["side"] = np.where(p.sponsor_majority.astype(bool), "majority", "minority")
    by_party = [
        {
            "side": side,
            "bin": k,
            "bills": len(x),
            "passed": r(x.passed.mean()) if len(x) else None,
            "ontp_report": r(x.ontp_report.mean()) if len(x) else None,
        }
        for side in ("majority", "minority")
        for k, x in p[p.side == side].groupby("bin", observed=False)
    ]
    party_totals = {
        side: {
            "bills": len(x),
            "passed": r(x.passed.mean()),
            "oppose_share": r(x.oppose_share.mean()),
            "ontp_report": r(x.ontp_report.mean()),
        }
        for side, x in p.groupby("side")
    }
    v = closed[closed.oppose_share >= 0.75].copy()
    v["size"] = pd.cut(v.voices, [1, 5, 20, 100, np.inf],
                       labels=["1-4", "5-19", "20-99", "100+"], right=False)  # fmt: skip
    by_volume = [
        {"voices": k, "bills": len(x), "passed": r(x.passed.mean())}
        for k, x in v.groupby("size", observed=False)
    ]
    despite = oc.passed_despite_opposition(b, n=10)
    return {
        "bills_with_status": len(b),
        "closed_positioned": len(closed),
        "closed_5_voices": len(d),
        "pass_rate_all": r(closed.passed.mean()),
        "outcome_mix": {k: int(v) for k, v in closed.outcome.value_counts().items()},
        "by_opposition": by_opp,
        "by_report": by_report,
        "by_party": by_party,
        "party_totals": party_totals,
        "by_volume": by_volume,
        "passed_despite": [
            {
                "session": int(x.session_number),
                "ld": int(x.ld),
                "title": x.title,
                "support": int(x.support),
                "oppose": int(x.oppose),
                "report": x.report,
            }  # fmt: skip
            for x in despite.itertuples()
        ],
        "points": [  # one dot per bill for the scatter (no names, just numbers)
            [r(x.oppose_share, 3), int(x.voices), int(x.passed), x.report] for x in d.itertuples()
        ],
    }


def bill_status_for_join(con) -> pd.DataFrame:
    s = pd.read_sql("SELECT session_number, ld, final_disposition, chapter FROM bill_status "
                    "WHERE error IS NULL", con)  # fmt: skip
    s["outcome"] = [
        oc.outcome_of(a, c) for a, c in zip(s.final_disposition, s.chapter, strict=True)
    ]
    return s[["session_number", "ld", "outcome"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data/testimony.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "docs/data/site.json"))
    args = ap.parse_args()

    store = Store(args.db)
    con = store.conn
    df = load(con)
    status = bill_status_for_join(con)
    pos = df.position.value_counts()
    methods = Counter(df.method.dropna())
    site = {
        "generated": date.today().isoformat(),
        "totals": {
            "submissions": len(df),
            "hearings": con.execute("SELECT COUNT(*) FROM hearings").fetchone()[0],
            "bills": int(df.groupby(["session_number", "paper_number"]).ngroups),
            "organizations": int(df[df.category.eq("organization")].org_canonical.nunique()),
            "towns": int(df[df.category.eq("town")].town.nunique()),
            "first_hearing": df.hearing_date.min(),
            "last_hearing": df.hearing_date.max(),
            "positions": {k: int(v) for k, v in pos.items()},
            "position_methods": dict(methods),
            "categories": {k: int(v) for k, v in df.category.value_counts().items()},
        },
        "accuracy": ACCURACY,
        "sessions": sessions_block(df, con),
        "monthly": monthly_block(df),
        "positions_by_group": positions_by_group(df),
        "volume": volume_block(df),
        "top_bills": top_bills_block(df, status),
        "campaigns": campaigns_block(df),
        "organizations": orgs_block(df),
        "org_pairs": org_pairs_block(df),
        "places": places_block(df),
        "repeat": repeat_block(df),
        "committees": committees_block(df, con),
        "outcomes": outcomes_block(store),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(site, separators=(",", ":"), default=str, allow_nan=False))
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
