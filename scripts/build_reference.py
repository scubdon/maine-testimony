"""Build src/maine_testimony/data/maine_places.csv from Census Gazetteer files.

Run once (or when the Census publishes a new vintage):

    python scripts/build_reference.py

Sources (public domain, US Census Bureau):
  * County subdivisions -> Maine's cities, towns, plantations, reservations
  * Places              -> census-designated places (villages people often give as "town")
  * Places, Census 2000 -> CDPs since dropped by the Census but still how people
                           describe where they live (Springvale, South Sanford)
"""

from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

VINTAGE = "2023"
BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{VINTAGE}_Gazetteer/"
COUSUB = BASE + f"{VINTAGE}_gaz_cousubs_23.txt"
PLACE = BASE + f"{VINTAGE}_gaz_place_23.txt"
# Fixed-width, all states; layout: www.census.gov/geo/www/gazetteer/places2k.html
PLACE_2000 = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/places2k.zip"
OUT = Path(__file__).resolve().parents[1] / "src/maine_testimony/data/maine_places.csv"

COUNTIES = {
    "001": "Androscoggin",
    "003": "Aroostook",
    "005": "Cumberland",
    "007": "Franklin",
    "009": "Hancock",
    "011": "Kennebec",
    "013": "Knox",
    "015": "Lincoln",
    "017": "Oxford",
    "019": "Penobscot",
    "021": "Piscataquis",
    "023": "Sagadahoc",
    "025": "Somerset",
    "027": "Waldo",
    "029": "Washington",
    "031": "York",
}
COUSUB_TYPES = {"town", "city", "plantation", "Reservation", "gore"}


def fetch(url: str) -> list[dict[str, str]]:
    with urllib.request.urlopen(url, timeout=60) as r:
        text = r.read().decode("utf-8")
    rows = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in rows]


def fetch_places_2000() -> list[dict[str, str]]:
    with urllib.request.urlopen(PLACE_2000, timeout=60) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read("places2k.txt").decode("latin-1")
    return [
        {
            "NAME": line[9:73].strip(),
            "GEOID": line[2:9],
            "ALAND_SQMI": line[119:131].strip(),
            "INTPTLAT": line[143:153].strip(),
            "INTPTLONG": line[153:164].strip(),
        }
        for line in text.splitlines()
        if line.startswith("ME")
    ]


def main() -> None:
    out: dict[str, dict[str, str]] = {}

    for r in fetch(COUSUB):
        name, _, kind = r["NAME"].rpartition(" ")
        if kind not in COUSUB_TYPES:
            continue  # skip unorganized-territory ("UT") aggregates
        if kind == "Reservation":
            name, kind = r["NAME"], "reservation"
        prev = out.get(name)
        if prev and float(prev["aland_sqmi"]) >= float(r["ALAND_SQMI"]):
            continue
        out[name] = {
            "name": name,
            "kind": kind.lower(),
            "county": COUNTIES.get(r["GEOID"][2:5], ""),
            "geoid": r["GEOID"],
            "lat": r["INTPTLAT"],
            "lon": r["INTPTLONG"],
            "aland_sqmi": r["ALAND_SQMI"],
        }

    # CDPs: only add names that are not already municipalities. Current vintage first;
    # 2000 only fills in dropped CDPs, skipping merged ones ("Mars Hill-Blaine").
    for r in fetch(PLACE) + fetch_places_2000():
        name, _, kind = r["NAME"].rpartition(" ")
        if kind != "CDP" or name in out or "-" in name:
            continue
        out[name] = {
            "name": name,
            "kind": "village",
            "county": "",
            "geoid": r["GEOID"],
            "lat": r["INTPTLAT"],
            "lon": r["INTPTLONG"],
            "aland_sqmi": r["ALAND_SQMI"],
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(out.values()))))
        w.writeheader()
        w.writerows(sorted(out.values(), key=lambda x: x["name"]))
    kinds: dict[str, int] = {}
    for v in out.values():
        kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1
    print(f"wrote {len(out)} places to {OUT}: {kinds}")


if __name__ == "__main__":
    main()
