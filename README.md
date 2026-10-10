# Who testifies in Augusta?

A scraper, cleaned dataset, and analysis of the written testimony submitted to Maine
Legislature committees, 2013 to the present.

Every bill in Maine gets a public hearing, and anyone can submit written testimony. The
Legislature posts that testimony online, one PDF at a time, behind a JavaScript page. This
project turns it into a structured dataset that can answer:

- **Which organizations weigh in most, on which issues, and on which side?**
- **Organized interests vs. the public.** On which bills do residents show up in force,
  and on which do lobbyists and agencies dominate?
- **Form-letter campaigns.** How much "public" testimony is copy-pasted templates?
- **Where civic participation comes from**, town by town.

> **Status:** pipeline complete and tested; full historical crawl in progress. Numbers
> below come from a sample of 104 hearings across all 17 committees of the 132nd
> Legislature (6,264 submissions, 250 PDFs; Agriculture is over-represented).

## How the source works

`legislature.maine.gov/bills/testimony.html` is a jQuery shell. Reading its scripts shows
it calls a handful of undocumented GET endpoints under `/bills/` that return JSON
(served as `text/javascript`):

| Endpoint | Params | Returns |
|---|---|---|
| `getSessionNumbersDesc.asp` | none | Legislatures (session number, years) |
| `getTestyCmte.asp` | `snum` | Committees with testimony, plus hearing-date counts |
| `getTestyCmteDates.asp` | `snum`, `comCode` | Hearing dates (`"Tue Feb 4 00:00:00 EST 2025"`) |
| `getTestyForCmteHearingDate.asp` | `hearingDate` (ISO), `comCode` | One record per submission |
| `getTestimonyDoc.asp` | `id` | The testimony PDF |

Each record has `id`, `firstName`, `lastName`, `organization`, `ld`, `paperNumber`,
`title`, `sessionNumber`, `policyArea`, `topic`. **There is no support/oppose field**, so
the position has to be read from the PDF. The `organization` field is free text, and most
members of the public type their town into it.

Coverage starts with the 126th Legislature (2013–14). Roughly 500–700 hearing dates per
session through 2022, then about 1,350 per session since.

## Pipeline

```
crawl ──► SQLite ──► classify ──► fetch-docs ──► positions ──► form-letters ──► export
(JSON)    (raw)     (who is it?)   (PDF→text)    (which side?)  (templates)    (public files)
```

```bash
pip install -e ".[dev]"

mtestimony crawl -s 132 --per-committee 4   # quick sample; omit options for everything
mtestimony classify
mtestimony fetch-docs --orgs-only --limit 500
mtestimony positions
mtestimony form-letters
mtestimony export                           # -> data/public/
```

The crawler is **incremental and resumable**. A hearing fetched more than 30 days after it
took place is treated as final and skipped on later runs. More recent hearings are
re-pulled, because written testimony keeps arriving after the hearing. A GitHub Actions
workflow (`.github/workflows/scrape.yml`) runs the whole pipeline weekly and commits
the refreshed public files.

### Being a polite scraper

- One request per second by default, at most two in flight, exponential backoff on
  5xx/429 responses.
- An identifying `User-Agent` with a contact URL (`MTESTIMONY_CONTACT`).
- `robots.txt` disallows bill search and LawMakerWeb. This project only uses the testimony
  endpoints, which robots.txt does not restrict.
- An optional raw-response cache, so re-processing never needs the network.

## Methodology

### Who is testifying? (`classify.py`)

The `organization` field is sorted into categories with ordered, auditable rules. Every
row records which rule fired.

| Category | Meaning | Example values | Sample share |
|---|---|---|---|
| `town` | Individual who gave a Maine municipality | `Portland`, `topsham`, `Bangor, Maine`, `Orringon`→Orrington | 51.8% |
| `organization` | Nonprofit, trade group, business, union | `Maine Audubon`, `MOFGA`, `MaineHealth` | 33.9% |
| `government` | State agency, municipality, county, school unit | `Department of Marine Resources`, `Town of Sanford` | 7.1% |
| `legislator` | Member of the Legislature | `Maine State Legislature` | 5.3% |
| `none` | Blank or placeholder | `N/A`, `test`, the person's own name | 0.8% |
| `place_other` | Individual from out of state | `Morgantown, West Virginia` | 0.1% |
| `unknown` | Needs review | `Waypoint` | 0.9% |

Town matching uses the Census Gazetteer list of Maine's 430 towns, 23 cities, plantations,
reservations and CDPs (`scripts/build_reference.py`). It handles `", ME 04xxx"` suffixes,
apostrophes and "St."/"Saint", directional villages (`West Baldwin`→Baldwin), townships,
compound values (`Wilton/MPAC` → an organization, with the town kept), and fuzzy matching
for typos in single-word values.

**Organizations** are normalized (case, punctuation, legal suffixes, "Assn."), mapped
through a small hand-kept alias table (`data/org_aliases.csv`), then fuzzy-clustered
(rapidfuzz `token_sort_ratio` ≥ 92). Ambiguous acronyms are deliberately not expanded:
"MMA" is both the Maine Municipal Association and the Maine Medical Association.

### Which side? (`positions.py`)

Maine testimony usually states its position in the header ("Testimony in Support of
LD 550", "Neither For Nor Against"). An ordered regex classifier:

1. checks *neither for nor against* first, since it contains both "for" and "against";
2. otherwise takes the **earliest** explicit support or oppose statement, so a header
   beats a later "we could support it if amended";
3. treats sponsors "introducing"/"presenting" their own bill as support (flagged
   `regex_sponsor`);
4. ignores "we support the *intent*" phrasing.

Every label stores its evidence snippet and character offset. In the 250-PDF sample, 80%
got a label, and a random spot check of 40 labels found all 40 correct. Most of the
remainder are letters that never state a position outright, such as budget testimony.
A proper hand-labeled evaluation is on the roadmap.

### Form-letter detection (`form_letters.py`)

For each bill, texts are normalized, with the submitter's own name and town tokens removed
because the online form prepends them. They're compared pairwise with rapidfuzz `ratio`
(vectorized `cdist`), and connected components at similarity ≥ 90 become campaign groups.
In the sample, all 31 downloaded LD 754 PDFs ("I oppose LD 754 and LD 1519, which bans…")
fell into a single group.

### Measuring accuracy (`evaluate.py`)

```bash
mtestimony label-sample --n 300      # writes data/labels/submitters_todo.csv
# fill in the `label` column by hand
mtestimony evaluate data/labels/submitters_todo.csv
```

This prints per-class precision and recall and the most common confusions, using the
*current* rules, so rule changes can be checked against the same labels. *(Results go here
once the labeled sample is done.)*

## The dataset (`data/public/`)

| File | Grain |
|---|---|
| `testimony.parquet` | One row per submission |
| `organizations.csv` | One row per organization cluster: submissions, bills, committees, support/oppose counts, first/last seen |
| `bills.csv` | One row per bill: submissions, from orgs vs. the public, support/oppose, form-letter count |
| `towns.csv` | Submissions per Maine town, with Census GEOID for mapping |

Key columns in `testimony.parquet`: `testimony_id`, `session_number`, `committee_code`,
`hearing_date`, `ld`, `paper_number`, `bill_title`, `submitter_category`,
`classification_rule`, `organization` (cluster display name; orgs and government only),
`town`, `town_geoid`, `legislator_name` (legislators only), `position`,
`position_method`, `form_letter_group_size`, `doc_url`.

## Privacy and ethics

Testimony is a public record, but most of it comes from private individuals, and the
PDFs often include home addresses, phone numbers, and personal stories. So:

- **The public dataset contains no names of private individuals and none of the raw
  free-text field.** Individuals appear only as a category plus, if given, a Maine town.
- **Legislators keep their names** because they testify in an official capacity.
  Organizations appear by name.
- **PDFs and their text are never redistributed.** Rows link to the original document
  on the Legislature's site.
- The working database, raw cache, PDFs, and label files are git-ignored. The scheduled
  workflow keeps the database in the Actions cache and commits only `data/public/`.

## Limitations

- The `organization` field is self-reported. Someone may list an employer while
  testifying personally, or a town while representing a group.
- The position classifier reads text, not intent, and leaves implicit positions as
  `unknown`. On omnibus bills such as the budget, a position is often about one part
  ("in opposition to Part UU of LD 210"), not the whole bill. Scanned PDFs without a
  text layer (about 1%) are skipped.
- The Legislature notes the online record "may not represent a complete record of a
  hearing."
- Budget and unprinted-bill testimony is served by a separate endpoint
  (`TestimonyByRequestId`) that is not crawled yet.

## Roadmap

- [ ] Full historical crawl (126th–133rd)
- [ ] Hand-label 300 submitters and 200 documents; publish accuracy
- [ ] LLM fallback for `unknown` positions, validated against the hand labels
- [ ] Join bill outcomes (passed, died, vetoed). Status pages are disallowed by
      robots.txt, so this needs another source.
- [ ] Findings write-up and interactive charts (GitHub Pages)

## Development

```bash
pip install -e ".[dev]"
pytest          # all HTTP is mocked; tests never touch the live site
ruff check . && ruff format --check .
```

Code: MIT. Data: derived from public records of the Maine State Legislature. Reference
geography: US Census Bureau (public domain).
