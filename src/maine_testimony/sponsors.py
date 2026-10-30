"""Primary sponsor and party per bill, from Open States (128th Legislature onward).

Two public-domain sources, both downloaded by hand into ``data/external/``:

* Per-session bill CSVs (open.pluralpolicy.com/data/session-csv/, needs a free login):
  bills keyed ``"LD 1834"``, one primary sponsor each, and the paper number in the bill
  text links (``getPDF.asp?paper=SP0716``). Open States scrapes these sponsors from
  LawMakerWeb; this project never crawls LawMakerWeb itself.
* ``openstates/people`` (CC0, GitHub): one YAML per legislator with dated terms and party.

From the 130th on, sponsors carry an ``ocd-person`` id. The 128th-129th files give an
uppercase surname only ("MASON"), so those are matched by surname among legislators
serving that session, then by chamber (an HP paper is a House sponsor, SP a Senate one).
Two members of one chamber with the same surname stay ``ambiguous``.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

_SESSION_IN_NAME = re.compile(r"ME_(\d+)_csv", re.I)
_PAPER_IN_URL = re.compile(r"paper=([HS]P)0*(\d+)", re.I)
_FAR = date(9999, 12, 31)


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    surnames: frozenset[str]  # normalized family_name / last word of name and other_names
    firsts: frozenset[str]  # normalized given_name / first word of name and other_names
    roles: tuple[tuple[str, date, date], ...]  # (upper | lower, start, end); no start = min
    parties: tuple[tuple[str, date | None, date | None], ...]  # (party, start, end)

    def serves(self, chamber: str | None, start: date, end: date) -> bool:
        return any(
            (chamber is None or c == chamber) and s <= end and e >= start for c, s, e in self.roles
        )

    def party_on(self, d: date) -> str | None:
        """The party in effect on ``d``; dated entries beat undated ones."""
        hits = [(p, s, e) for p, s, e in self.parties if (s or date.min) <= d <= (e or _FAR)]
        if not hits:
            return None
        hits.sort(key=lambda h: h[1] is None and h[2] is None)
        return hits[0][0]


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", s)
    return re.sub(r"[^a-z ]", "", s).strip()


def _words(name: str) -> list[str]:
    """Name words with "of <Town>" dropped and "Last, First" put back in order."""
    name = re.split(r"\s+of\s+", name, maxsplit=1)[0]
    last, comma, first = name.partition(",")
    return _norm(f"{first} {last}" if comma else name).split()


def _surname(name: str) -> str:
    words = _words(name)
    return words[-1] if words else ""


def _first(name: str) -> str:
    """First word if there is more than one ("Troy JACKSON" -> "troy"; "MASON" -> "")."""
    words = _words(name)
    return words[0] if len(words) > 1 else ""


def _date(v: object) -> date | None:
    if v is None or v == "":
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def load_people(people_dir: Path) -> dict[str, Person]:
    """``openstates/people`` ``data/me``: its ``legislature/`` and ``retired/`` YAML files."""
    import yaml  # optional extra: pip install -e ".[sponsors]"

    out: dict[str, Person] = {}
    for path in sorted(people_dir.glob("*/*.yml")):
        if path.parent.name not in ("legislature", "retired"):
            continue
        p = yaml.safe_load(path.read_text("utf-8"))
        roles = tuple(
            (r["type"], _date(r.get("start_date")) or date.min, _date(r.get("end_date")) or _FAR)
            for r in p.get("roles", [])
            if r.get("type") in ("upper", "lower")
        )
        if not roles:
            continue
        names = [p.get("family_name") or "", p["name"]]
        names += [o["name"] for o in p.get("other_names", []) if o.get("name")]
        surnames = frozenset(s for n in names if (s := _surname(n)))
        firsts = frozenset(
            f for n in [p.get("given_name") or "", *names[1:]] if (f := (_words(n) or [""])[0])
        )
        parties = tuple(
            (x["name"], _date(x.get("start_date")), _date(x.get("end_date")))
            for x in p.get("party", [])
        )
        out[p["id"]] = Person(p["id"], p["name"], surnames, firsts, roles, parties)
    return out


def session_window(session: int) -> tuple[date, date]:
    """A Legislature is sworn in early December of an even year (128th: Dec 2016)."""
    year = 2016 + 2 * (session - 128)
    return date(year, 12, 1), date(year + 2, 11, 30)


def session_of(path: Path) -> int:
    m = _SESSION_IN_NAME.search(path.name)
    if not m:
        raise ValueError(f"can't tell the session from {path.name!r} (expected ME_<n>_csv...)")
    return int(m.group(1))


def _read(source: Path, suffix: str) -> pd.DataFrame:
    """One CSV of a session export, from the unzipped folder or the zip itself."""
    if source.is_dir():
        hits = list(source.rglob(f"*{suffix}"))
        if not hits:
            raise FileNotFoundError(f"no *{suffix} under {source}")
        return pd.read_csv(hits[0], dtype=str, keep_default_na=False)
    with zipfile.ZipFile(source) as z:
        name = next((n for n in z.namelist() if n.endswith(suffix)), None)
        if name is None:
            raise FileNotFoundError(f"no *{suffix} in {source}")
        return pd.read_csv(io.BytesIO(z.read(name)), dtype=str, keep_default_na=False)


def primary_sponsors(source: Path) -> pd.DataFrame:
    """``ld, paper_number, sponsor_name, os_person_id`` for each LD bill in one session."""
    bills = _read(source, "_bills.csv")
    sp = _read(source, "_bill_sponsorships.csv")
    versions = _read(source, "_bill_versions.csv")
    links = _read(source, "_bill_version_links.csv")

    bills["ld"] = pd.to_numeric(bills.identifier.str.extract(r"^LD (\d+)$")[0])
    bills = bills.dropna(subset=["ld"])  # orders and joint resolutions have no LD

    links = links.merge(versions[["id", "bill_id"]], left_on="version_id", right_on="id")
    m = links.url.str.extract(_PAPER_IN_URL)
    links["paper"] = m[0].str.upper() + m[1].str.zfill(4)
    paper = links.dropna(subset=["paper"]).groupby("bill_id").paper.first()

    prim = sp[sp.primary == "True"].drop_duplicates("bill_id").set_index("bill_id")
    out = pd.DataFrame(
        {
            "ld": bills.ld.astype(int).to_numpy(),
            "paper_number": bills.id.map(paper).to_numpy(),
            "sponsor_name": bills.id.map(prim.name).to_numpy(),
            "os_person_id": bills.id.map(prim.person_id).replace("", None).to_numpy(),
        }
    )
    return out.drop_duplicates("ld").dropna(subset=["sponsor_name"])


def _narrow(cands: list[Person], keep) -> list[Person]:
    """Apply a filter only when it leaves someone (a presiding officer may sit elsewhere)."""
    kept = [p for p in cands if keep(p)]
    return kept or cands


def _first_matches(first: str, p: Person) -> bool:
    if len(first) == 1:  # an initial: "W. CROCKETT"
        return any(f.startswith(first) for f in p.firsts)
    return first in p.firsts


def resolve(
    sponsors: pd.DataFrame, people: dict[str, Person], session: int
) -> list[dict[str, object]]:
    """Attach person and party to each bill's primary sponsor.

    ``match``: ``person_id`` (Open States linked it) | ``surname`` + ``_chamber`` /
    ``_first`` for each filter needed to get one candidate | ``same_party`` (several
    candidates, e.g. duplicate records of one person, all in one party: party kept, person
    not) | ``ambiguous`` | ``unmatched``.
    """
    start, end = session_window(session)
    party_day = date(start.year + 1, 2, 1)
    serving = [p for p in people.values() if p.serves(None, start, end)]
    rows = []
    for r in sponsors.itertuples(index=False):
        person, party, match = people.get(r.os_person_id or ""), None, "person_id"
        if person is None:
            surname, first = _surname(r.sponsor_name), _first(r.sponsor_name)
            cands = [p for p in serving if surname in p.surnames] if surname else []
            match = "surname"
            if len(cands) > 1 and isinstance(r.paper_number, str):
                chamber = "lower" if r.paper_number.startswith("HP") else "upper"
                n = len(cands)
                cands = _narrow(cands, lambda p, c=chamber: p.serves(c, start, end))
                match += "_chamber" if len(cands) < n else ""
            if len(cands) > 1 and first:
                n = len(cands)
                cands = _narrow(cands, lambda p, f=first: _first_matches(f, p))
                match += "_first" if len(cands) < n else ""
            if len(cands) == 1:
                person = cands[0]
            elif cands:
                parties = {p.party_on(party_day) for p in cands} - {None}
                if len(parties) == 1:
                    party, match = parties.pop(), "same_party"
                else:
                    match = "ambiguous"
            else:
                match = "unmatched"
        rows.append(
            {
                "session_number": session,
                "ld": int(r.ld),
                "paper_number": r.paper_number if isinstance(r.paper_number, str) else None,
                "sponsor_name": r.sponsor_name,
                "person_id": person.id if person else None,
                "person_name": person.name if person else None,
                "party": person.party_on(party_day) if person else party,
                "match": match,
            }
        )
    return rows
