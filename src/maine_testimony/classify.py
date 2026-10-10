"""Classify the free-text ``organization`` field of a testimony submission.

The upstream form asks for an organization, but most individual members of the
public type their town ("Portland", "topsham", "Bangor, Maine"). This module sorts
each value into one of:

=================  ==========================================================
``legislator``     A member of the Legislature (public official)
``government``     State agency, municipality as an entity, county, school unit
``organization``   Nonprofit, trade group, business, advocacy group, union...
``town``           An individual who gave a Maine municipality / village
``place_other``    An individual who gave an out-of-state place
``none``           Blank, "N/A", "self", "private citizen", their own name...
``unknown``        Could not decide; send to manual review
=================  ==========================================================

Rules run in a fixed order and every result records which rule fired, so the
classification can be audited and the rules tuned against a labeled sample.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import resources

from rapidfuzz import fuzz, process


class Category(StrEnum):
    LEGISLATOR = "legislator"
    GOVERNMENT = "government"
    ORGANIZATION = "organization"
    TOWN = "town"
    PLACE_OTHER = "place_other"
    NONE = "none"
    UNKNOWN = "unknown"


ORG_LIKE = {Category.LEGISLATOR, Category.GOVERNMENT, Category.ORGANIZATION}


@dataclass(frozen=True)
class Classification:
    category: Category
    rule: str
    town: str | None = None
    town_geoid: str | None = None


# --------------------------------------------------------------------- patterns
_NONE = re.compile(
    r"^(n/?a|na|none|null|no|nil|self|myself|me|maine|personal|individual|private( citizen)?|"
    r"citizen|resident|constituent|retired|concerned citizen|private individual|"
    r"test|testing|unaffiliated|independent|-+|\.+|x+|\?+)$",
    re.I,
)
_LEGISLATOR = re.compile(
    r"\b(maine )?(state )?legislature\b|\bhouse of representatives\b|"
    r"\bmaine (state )?(house|senate)\b|^(senate|house)$|"
    r"^(state )?(rep(resentative)?|sen(ator)?)\.?\b|\b(house|senate) district\b",
    re.I,
)
_GOVERNMENT = re.compile(
    r"^(maine )?(department|dept\.?|bureau|office) of\b|\bdepartment of\b|"
    r"^(town|city|county) of\b|\b(county (commissioners?|government|sheriff'?s? office))\b|"
    r"\bsheriff'?s? office\b|\bpolice department\b|\bfire department\b|"
    r"\bgovernor'?s? office\b|^office of the governor\b|\battorney general\b|"
    r"\bsecretary of state\b|\bpublic utilities commission\b|\bpublic advocate\b|"
    r"\b(state|maine) (police|library|museum|archives|housing authority)\b|"
    r"\b(permanent )?comm?iss?ion on\b|\bmaine (state )?board of\b|\bjudicial branch\b|"
    r"\b(finance|turnpike|port|housing|transit|water|sewer) authority\b|\bauthority of maine\b|"
    r"\b(rsu|msad|sad|aos)\s*#?\s*\d+\b|\bschool (department|district|board|committee)\b|"
    r"^(dhhs|dacf|dep|mdot|doe|difw|dmr|dafs|dps|doc|decd|dol|puc|maine ?cdc|maine ?dot)$",
    re.I,
)
_ORG_WORDS = re.compile(
    r"\b(associat\w*|assn|coalition|council|alliance|institute|society|network|foundation|"
    r"federation|league|union|guild|club|chamber|partnership|partners|cooperative|co-?op|"
    r"center|centre|services|agency|group|project|fund|trust|campaign|initiative|"
    r"action|advocates?|caucus|committee|board|commission|collaborative|consortium|"
    r"inc|llc|corp|company|co\b|ltd|enterprises?|industries|farms?|bank|credit union|"
    r"hospital|health|healthcare|clinic|medical|university|college|academy|school|"
    r"church|parish|diocese|congregation|ministr\w+|nation|tribe|tribal|band of|"
    r"aclu|afl-?cio|seiu|ymca|ywca|audubon|sierra club|friends of|maine people'?s|"
    r"party|democrats?|republicans?|libertarians?|greens?|policy|press|media|news|"
    r"sanctuary|museum|library|libraries|theater|theatre|arts?|organization|org)\b",
    re.I,
)
_ACRONYM = re.compile(r"^[A-Z][A-Z&\-]{1,8}$")
_CAMEL = re.compile(r"^[A-Z][a-z]+(?:[A-Z][A-Za-z]*)+$")  # MaineHealth, FocusMaine
_TOWNSHIP = re.compile(r"^(.+?)\s+(twp\.?|township|plt\.?|plantation)$", re.I)
_ME_SUFFIX = re.compile(r"[,\s]+(me|maine)\.?(\s+\d{5}(-\d{4})?)?$", re.I)
_DIRECTIONAL = re.compile(r"^(north|south|east|west|n\.?|s\.?|e\.?|w\.?)\s+(.+)$", re.I)

_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    "oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|"
    "vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    "al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|md|ma|mi|mn|ms|mo|mt|ne|nv|"
    "nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc"
)
_OTHER_PLACE = re.compile(rf"^[a-z .'\-]+,\s*({_US_STATES})\.?(\s+\d{{5}})?$", re.I)


# ------------------------------------------------------------------- places
@dataclass(frozen=True)
class Place:
    name: str
    kind: str
    geoid: str


def place_key(name: str) -> str:
    """Comparison key for place names: "Swan's Island" == "Swans Island",
    "Saint George" == "St. George" == "st george"."""
    s = name.casefold().replace("'", "").replace("’", "").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^saint ", "st ", s)


@lru_cache(maxsize=1)
def maine_places() -> dict[str, Place]:
    """place_key -> Place, from the bundled Census-derived CSV."""
    path = resources.files("maine_testimony") / "data" / "maine_places.csv"
    with path.open() as f:
        rows = list(csv.DictReader(f))
    return {place_key(r["name"]): Place(r["name"], r["kind"], r["geoid"]) for r in rows}


def match_town(value: str) -> tuple[Place, str] | None:
    """Return ``(place, rule)`` if the value is a Maine municipality or village."""
    places = maine_places()
    s = place_key(_ME_SUFFIX.sub("", value.strip()).strip(" ,."))
    if not s:
        return None
    if s in places:
        return places[s], "town_exact"
    m = _DIRECTIONAL.match(s)
    if m and m.group(2) in places:
        return places[m.group(2)], "town_directional"  # e.g. "West Baldwin" -> Baldwin
    if len(s) >= 5 and " " not in s.strip():
        hit = process.extractOne(s, list(places), scorer=fuzz.ratio, score_cutoff=88)
        if hit:
            return places[hit[0]], "town_fuzzy"  # typos: "Orringon", "Castin"
    return None


# ------------------------------------------------------------------- classify
_PRIORITY = [
    Category.LEGISLATOR,
    Category.GOVERNMENT,
    Category.ORGANIZATION,
    Category.TOWN,
    Category.PLACE_OTHER,
    Category.UNKNOWN,
    Category.NONE,
]


def classify(organization: str, first_name: str = "", last_name: str = "") -> Classification:
    raw = " ".join((organization or "").split())
    if not raw or _NONE.match(raw):
        return Classification(Category.NONE, "blank_or_placeholder")

    full = f"{first_name} {last_name}".strip().casefold()
    if full and raw.casefold() in {full, f"{last_name} {first_name}".strip().casefold()}:
        return Classification(Category.NONE, "own_name")

    # "Wilton/MPAC", "BRILUP/DACF": classify each part, keep the most
    # institutional one, but remember the town if one part was a town.
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if 2 <= len(parts) <= 3 and not _TOWNSHIP.match(raw):
        subs = [_classify_one(p) for p in parts]
        best = min(subs, key=lambda c: _PRIORITY.index(c.category))
        town = next((c for c in subs if c.town), None)
        return Classification(
            best.category,
            f"slash:{best.rule}",
            town.town if town else best.town,
            town.town_geoid if town else best.town_geoid,
        )
    return _classify_one(raw)


def _classify_one(raw: str) -> Classification:
    if _NONE.match(raw):
        return Classification(Category.NONE, "blank_or_placeholder")
    if _LEGISLATOR.search(raw):
        return Classification(Category.LEGISLATOR, "legislator_pattern")
    if _GOVERNMENT.search(raw):
        return Classification(Category.GOVERNMENT, "government_pattern")

    town = match_town(raw)
    if town:
        place, rule = town
        return Classification(Category.TOWN, rule, place.name, place.geoid)

    if m := _TOWNSHIP.match(raw):
        # Unorganized townships ("Argyle Twp.") and plantations: a place, not an org.
        place = match_town(m.group(1)) or match_town(raw)
        return Classification(
            Category.TOWN,
            "township",
            place[0].name if place else m.group(1).title(),
            place[0].geoid if place else None,
        )
    if _OTHER_PLACE.match(raw):
        return Classification(Category.PLACE_OTHER, "out_of_state_place")
    if _ORG_WORDS.search(raw):
        return Classification(Category.ORGANIZATION, "org_keyword")
    if _ACRONYM.match(raw):
        return Classification(Category.ORGANIZATION, "acronym")
    if _CAMEL.match(raw):
        return Classification(Category.ORGANIZATION, "camelcase")
    if len(raw.split()) >= 2 and not raw.islower():
        # Multi-word, capitalized, not a place: most often a business or group
        # name ("Kennebec Cheesery"). Low confidence; filter on `rule` if needed.
        return Classification(Category.ORGANIZATION, "fallback_multiword")
    return Classification(Category.UNKNOWN, "no_rule")
