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

> **Status:** pipeline complete and tested; full historical crawl in progress. Category
> shares below cover everything crawled so far: 181,131 submissions from 4,083 hearings
> in the 126th–132nd Legislatures (October 2026). Figures marked "sample" come from an
> earlier 104-hearing sample of the 132nd.

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
mtestimony fetch-docs --orgs-only --limit 500   # OCRs new scans if ocrmypdf is installed
mtestimony positions
mtestimony form-letters
mtestimony fetch-status                     # each bill's committee report + final outcome
mtestimony outcomes                         # pass rate by share of testimony opposed
mtestimony export                           # -> data/public/
```

`fetch-docs` records a failed download or unreadable PDF in the database instead of
stopping. `mtestimony fetch-docs --retry-errors` tries only those again
(scans and documents that already have text, including OCR, are left alone).

The crawler is **incremental and resumable**. A hearing fetched more than 30 days after it
took place is treated as final and skipped on later runs. More recent hearings are
re-pulled, because written testimony keeps arriving after the hearing. The public files
in `data/public/` are exported from the maintainer's local database and committed by
hand. A GitHub Actions workflow (`.github/workflows/scrape.yml`) is a light weekly
health check: it crawls new listings, downloads a sample of 20 PDFs and runs every later
step, failing if the site's endpoints stop answering as expected. It commits nothing.

### Being a polite scraper

- One request per second by default, at most two in flight, exponential backoff on
  5xx/429 responses.
- An identifying `User-Agent` with a contact URL (`MTESTIMONY_CONTACT`).
- `robots.txt` disallows bill search (`default_ps.asp`, `search_ps.asp`) and LawMakerWeb.
  This project uses only the testimony endpoints and the per-bill status page
  (`display_ps.asp`), which robots.txt does not restrict.
- An optional raw-response cache, so re-processing never needs the network.

## Methodology

### Who is testifying? (`classify.py`)

The `organization` field is sorted into categories with ordered, auditable rules. Every
row records which rule fired.

| Category | Meaning | Example values | Share |
|---|---|---|---|
| `town` | Individual who gave a Maine municipality or a village in one | `Portland`, `topsham`, `Bangor, Maine`, `Orringon`→Orrington | 42.4% |
| `organization` | Nonprofit, trade group, business, union | `Maine Audubon`, `MOFGA`, `MaineHealth` | 37.3% |
| `government` | State agency, board or commission, municipality, county, school unit, utility district | `Department of Marine Resources`, `Town of Sanford` | 8.8% |
| `legislator` | Member of the Legislature | `Maine State Legislature` | 7.7% |
| `none` | Blank or placeholder | `N/A`, `test`, the person's own name | 2.4% |
| `place_other` | Individual from out of state | `Morgantown, West Virginia` | 0.3% |
| `unknown` | Needs review | `Waypoint` | 1.1% |

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

Every label stores its evidence snippet and character offset. On a held-out, hand-labeled
sample of 200 letters, the regexes give a label to 82% and are right 95.1% of the time
when they do; 33 of the 196 letters that do state a position are left `unknown` (see
[Measuring accuracy](#measuring-accuracy-evaluatepy)).

`mtestimony positions --llm` (opt-in, needs `ANTHROPIC_API_KEY` and `pip install -e
".[llm]"`) sends the documents the regexes leave `unknown` to Claude Haiku: only the
first 3,000 characters, with output constrained to support / oppose / neither / unknown.
The model must quote the sentence that states the stance, and the quote is checked
against the text; an answer without a matching quote stays `unknown`
(`llm_unverified`). Answers are cached in the database by text hash, so reruns and
identical form letters don't cost extra; `--llm-limit N` caps API calls per run. The
rare letters the model's safety filters decline (5 of 6,758 calls on the 132nd) are
cached as refusals and stay `unknown` (`llm_refused`).
Exports carry `position_method = llm`, never the document text.
On the held-out sample of 200 letters, accuracy goes from 79.5% (regex only) to 91.5%
and missed positions from 33 to 7 of 196, while precision when a label is given barely
moves (95.1% → 94.7%).

### Scanned PDFs (`ocr.py`)

About 2% of the PDFs are scans with no text layer, and they are not spread evenly:
closer to 3% of organization and government letters (official letterhead, signed and
scanned) against under 1% from individuals. Without OCR these have no position, so
institutional voices would be under-counted. Two routes fill the same text column, and
`documents.text_source` records which one did (`pdf`, `ocrmypdf`, or the tool you name):

- **Automated.** With `pip install -e ".[ocr]"` and Tesseract (`brew install tesseract`
  / `apt install tesseract-ocr`), `fetch-docs` OCRs each new scan with
  [ocrmypdf](https://ocrmypdf.readthedocs.io/). `mtestimony ocr --limit N` does the
  same for scans already in the database (PDFs aren't stored, so each is downloaded
  again).
- **External tool.** `mtestimony export-scans` saves every scan still lacking text to
  `data/scans/<testimony_id>.pdf`. Run them through ABBYY FineReader, DEVONthink or
  similar, keeping the id at the start of each file name (searchable PDF or `.txt`), then
  `mtestimony import-ocr DIR --source finereader`.

OCR text never replaces an embedded text layer. Like all document text, it stays in the
working database and is never exported. Rerun `positions` and `form-letters` afterwards.

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
*current* rules, so rule changes can be checked against the same labels.
`label-sample --kind positions` does the same for positions (one document per form-letter
group). `--exclude FILE` keeps earlier label files out of a new sample.

**Results (October 2026).** Both samples were drawn after rule tuning was finished and
share no rows with the label sets used for tuning. The submitter sample covers the
126th–132nd Legislatures. The positions sample is from the 132nd only, since that is the
only session whose PDFs have been downloaded so far.

Submitters, n = 300: **95.3% accuracy**.

| Category | Precision | Recall | n |
|---|---|---|---|
| `town` | 1.00 | 0.97 | 132 |
| `organization` | 0.90 | 0.98 | 102 |
| `government` | 1.00 | 0.79 | 28 |
| `legislator` | 1.00 | 1.00 | 24 |
| `none` | 1.00 | 0.86 | 14 |

Most errors land in `organization`. Six state and local bodies have names the
government patterns miss (`Maine Arts Commission`, `Board of Licensure in Medicine`,
`MainePERS`, `Portland Water District`, `Prosecutorial District 3`, `Dept. Inland
Fisheries and Wildlife`). Four villages inside a larger town (such as `Port Clyde` and
`Cundy's Harbor`) aren't in the Census town list. So `government` is undercounted by about
a fifth, and `organization` is slightly inflated.

Positions, n = 200 (196 letters that state a position, 4 that don't):

| | Accuracy | Precision when labeled | Positioned letters missed |
|---|---|---|---|
| Regex only | 79.5% | 95.1% | 33 / 196 |
| Regex + `--llm` | **91.5%** | 94.7% | 7 / 196 |

The regexes rarely mistake one side for the other: most of their errors are letters left
`unknown`, which is the gap the LLM fallback closes.

### Does testimony track the outcome? (`outcomes.py`)

`fetch-status` reads each bill's status page (13,181 bills with testimony, fetched
2026-10-29; 13,154 had a final outcome). `outcomes` joins it to the positions. In this
section a "voice" is one non-legislator letter, with each form-letter campaign counted
once. A bill's opposition share is oppose / (support + oppose). The tables below cover
the 5,947 closed bills with at least 5 voices; overall, 41% of the 12,817 closed bills
with positioned testimony became law.

| Share opposed | Bills | Became law | vs. committee avg | Committee said ONTP |
|---|---|---|---|---|
| under 10% | 1,903 | 50.7% | +9.9 pts | 14.4% |
| 10-25% | 1,176 | 46.9% | +4.6 | 21.4% |
| 25-50% | 1,059 | 36.1% | −7.6 | 25.8% |
| 50-75% | 983 | 23.5% | −18.6 | 34.5% |
| 75-90% | 596 | 16.8% | −25.2 | 37.9% |
| 90% or more | 230 | 13.0% | −27.5 | 52.2% |

"vs. committee avg" compares each bill with the other bills in the same committee and
session. So the drop isn't just that some committees kill more bills.

**Bills that are mostly opposed rarely pass, but the link runs through the committee.**
Among bills with the same committee report, the share opposed barely changes the pass
rate:

| Share opposed | Unanimous OTP | Split, OTP majority | Split, ONTP majority |
|---|---|---|---|
| under 10% | 71% | 52% | 4% |
| 25-50% | 82% | 53% | 2% |
| 75-90% | 76% | 42% | 0% |
| 90% or more | 80% | 52% | 0% |

So heavy opposition goes with committees voting the bill down. Once a committee
recommends a bill, the floor follows the committee, not the testimony. The low 71% for
favored, uncontroversial bills is mostly bills that "died on adjournment" after an OTP
report (14% in that bin), usually because they weren't funded.

More voices don't help opponents. Among bills at least 75% opposed, those with 100+
distinct voices passed more often (28%) than those with 5-19 (14%). Large campaigns
form around high-profile bills, which often already have majority backing. The bills
passed over the most opposition were the 131st's abortion and health-care shield bills
(LD 1619, LD 1343, LD 227), the 129th's vaccine-exemption repeal (LD 798) and
ranked-choice voting for presidential elections (LD 1083). All five had split committee
reports. Counting every form letter (`--raw`) gives the same pattern.

These are associations, not causal effects:
- Opponents mobilize where a bill might pass, and lawmakers and testifiers react to the
  same features of a bill (party, cost, who is asking).
- Testimony could shape the committee vote, or both could reflect the bill's politics.
  This data can't tell which.
- Sponsor and party aren't joined (they're on LawMakerWeb, which robots.txt disallows),
  so partisanship isn't controlled for.

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
  workflow keeps its own database in the Actions cache and commits nothing.

## Limitations

- The `organization` field is self-reported. Someone may list an employer while
  testifying personally, or a town while representing a group.
- The position classifier reads text, not intent, and leaves implicit positions as
  `unknown`. On omnibus bills such as the budget, a position is often about one part
  ("in opposition to Part UU of LD 210"), not the whole bill. Scanned PDFs (about 2.4%)
  depend on OCR, which can misread faint or handwritten pages; the few that stay
  unreadable have no position. So do 145 uploads that the site serves as empty PDFs
  (zero pages), and about 20 that fail to download or aren't PDFs.
- The Legislature notes the online record "may not represent a complete record of a
  hearing."
- Bill outcomes are joined on (session, LD). 9 LDs had no status page and 18 were still
  open at fetch time. Bills with no positioned testimony aren't in the analysis, so the
  pass rates describe bills people testified on, not all bills.
- Budget and unprinted-bill testimony is served by a separate endpoint
  (`TestimonyByRequestId`) that is not crawled yet.

## Roadmap

- [ ] Full historical crawl (126th–133rd)
- [x] Hand-label 300 submitters and 200 documents; publish accuracy
- [x] LLM fallback for `unknown` positions, validated against the hand labels
- [x] Join bill outcomes (committee report, passed, died, vetoed) from each bill's
      status page
- [ ] Bill outcomes in `data/public/bills.csv`
- [ ] Findings write-up and interactive charts (GitHub Pages)

## Development

```bash
pip install -e ".[dev]"
pytest          # all HTTP is mocked; tests never touch the live site
ruff check . && ruff format --check .
```

Code: MIT. Data: derived from public records of the Maine State Legislature. Reference
geography: US Census Bureau (public domain).
