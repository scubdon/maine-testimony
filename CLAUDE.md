# CLAUDE.md

Scraper + dataset pipeline for written testimony submitted to Maine Legislature
committees (126th Legislature, 2013, to present). Python package `maine_testimony`,
CLI `mtestimony`. README.md is the human-facing overview; this file is the working
context for making changes.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                          # 207 tests, ~2s, all HTTP + Claude API + OCR mocked
ruff check . && ruff format .      # line length 100, target py311

# Small live run (a few minutes at 1 req/sec)
mtestimony crawl -s 132 --per-committee 2
mtestimony classify
mtestimony fetch-docs --orgs-only --limit 100
mtestimony fetch-docs --bill 132:SP0262          # or: -s 132 --min-submissions 300
mtestimony ocr --limit 50          # scans already in the DB (needs the [ocr] extra + tesseract)
mtestimony export-scans            # -> data/scans/<id>.pdf for FineReader etc.
mtestimony import-ocr DIR --source finereader
mtestimony positions                # --llm: Claude for regex-unknowns (opt-in, cached)
mtestimony form-letters
mtestimony export                  # -> data/public/
mtestimony stats
```

Every command takes `--db PATH` (default `data/testimony.sqlite`).
`mtestimony -v <cmd>` turns on INFO logging.

## Architecture

```
legislature.maine.gov -> client.py -> crawl.py / fetch-docs -> SQLite (raw tables)
SQLite raw -> classify.py / positions.py / form_letters.py -> SQLite (derived tables)
SQLite (all) -> pipeline.export_public -> data/public/
```

| Module | Role |
|---|---|
| `models.py` | pydantic models for upstream JSON (camelCase aliases, frozen, extra ignored) |
| `client.py` | the ONLY code that touches the network: rate limit, semaphore, retries, disk cache |
| `db.py` | `Store` + SCHEMA; all SQL writes/reads live here |
| `crawl.py` | sessions -> committees -> hearing dates -> testimony; incremental via `hearings` table |
| `classify.py` | free-text `organization` field -> category + the rule that fired |
| `orgs.py` | org-name normalization (`org_key`) + fuzzy clustering |
| `positions.py` | PDF text extraction (pypdf) + ordered regex support/oppose/neither |
| `ocr.py` | scanned PDFs: `ocr_pdf` runs ocrmypdf as a subprocess (sidecar text only); file-name/text helpers for `import-ocr` |
| `llm_positions.py` | opt-in Claude fallback for regex `unknown`s; `ClaudePositions` is the only API caller; answers cached in `llm_positions` |
| `form_letters.py` | near-duplicate texts per bill (rapidfuzz `cdist` + union-find) |
| `pipeline.py` | runs derived steps; `export_public` builds the privacy-safe files |
| `evaluate.py` | hand-label sampling + precision/recall scoring |
| `cli.py` | Typer commands; thin wrappers, logic stays in modules |
| `data/` | `maine_places.csv` (built by `scripts/build_reference.py`), `org_aliases.csv` |

**Raw vs derived is the core design.** Raw tables (`sessions`, `committees`,
`hearings`, `testimony`, `documents`) hold what the site returned. Derived tables
(`submitters`, `positions`, `form_letters`) are DELETED AND FULLY REBUILT on each
run of their step. So rule changes only need the derived step rerun, never a
re-crawl. Everything joins on `testimony_id` (= upstream id = PDF id).

## Upstream API facts (undocumented; found by reading the page's jQuery)

Base `https://legislature.maine.gov/bills/`:
- `getSessionNumbersDesc.asp` · `getTestyCmte.asp?snum=` ·
  `getTestyCmteDates.asp?snum=&comCode=` ·
  `getTestyForCmteHearingDate.asp?hearingDate=YYYY-MM-DD&comCode=` ·
  `getTestimonyDoc.asp?id=` (PDF)
- `comCode` must be the SHORT code (`HHS`). The committee's full name silently
  returns `[]`.
- Dates come back as Java strings (`"Tue Feb 4 00:00:00 EST 2025"`); parse with
  `models.parse_presented_date`, send back as ISO.
- JSON is served as `text/javascript`: parse `resp.text`, don't trust content type.
- `ld == 0` means no LD number (budget/unprinted); models convert it to `None`.
- No support/oppose field exists upstream; positions come only from PDF text.
- `robots.txt` disallows `default_ps.asp`, `search_ps.asp`, LawMakerWeb. Do not
  scrape those (this is why bill outcomes aren't joined yet).
- Budget/unprinted testimony uses another endpoint (`TestimonyByRequestId`),
  not crawled yet.

## Rules that must not break

**Privacy**
- The working DB, `data/raw/`, PDFs and label files contain private people's
  names, addresses and personal stories. Never commit anything under `data/`
  except the allowlisted files in `data/public/` (see `.gitignore`).
- `export_public` never selects `first_name`, `last_name` or raw `organization`.
  Names are exported only for `legislator`. Individuals = category + matched town.
- Never redistribute PDF text; export links (`doc_url`) instead. OCR text is the same.
- `export-scans` writes PDFs to `data/scans/` (gitignored with the rest of `data/`).
- `test_pipeline.py::test_end_to_end` guards this. Keep it passing; extend it if
  you add export columns.
- Test fixtures use fictional names only.

**Politeness**
- Default `min_interval=1.0` s, concurrency 2. Don't lower these defaults.
- Keep the identifying User-Agent (`MTESTIMONY_CONTACT` env var;
  `client.py` still has a `YOUR-USER` placeholder).
- All network access goes through `TestimonyClient`. Tests never hit the live
  site: use the `mock_api` fixture (respx) in `tests/conftest.py`.

**Classification**
- Rule ORDER in `classify._classify_one` is load-bearing: placeholder -> legislator
  -> government (pattern, `public_school`, `town_schools`) -> Maine town
  (incl. `town_multi` lists) -> township -> out-of-state -> org keyword ->
  acronym -> CamelCase -> multiword fallback -> unknown. E.g. government before
  town keeps "Town of Sanford" a government; town before org keyword keeps
  "Union" (a Knox County town) a town. Government patterns are skipped when the
  name contains "association" (an association of governments is an organization).
- Every result carries a `rule` string saved to the DB. New rules need a name.
- Never auto-expand acronyms ("MMA" = Municipal OR Medical Association). Only
  unambiguous ones go in `org_aliases.csv`.
- Positions: NFNA checked first; then earliest support/oppose match wins; sponsor
  language counts as support (`regex_sponsor`). Always store evidence snippet.
- LLM fallback (`positions --llm`): only regex `unknown` docs, only `head_text()` (first
  `HEAD_CHARS`) is sent. `llm_positions` is a cache, NOT derived: never delete it casually,
  it is what keeps reruns from re-billing. Its key includes `PROMPT_VERSION` (a hash of the
  prompt + schema), so editing the prompt re-asks everything. An answer whose evidence
  quote isn't found in the text becomes `unknown` / `llm_unverified`. Safety refusals
  (Haiku has no server-side fallback) are cached as `refused` + `refusal_category` and apply
  as `unknown` / `llm_refused`; transient errors are not cached. `Store._migrate` adds
  columns to existing DBs. Tests use fakes; the
  autouse conftest fixture hides API keys. Don't tune the prompt on the held-out labels.

**OCR**
- `documents.text_source`: `pdf` (embedded layer) | `ocrmypdf` | the `import-ocr --source`
  name. Scans without text have `error` = `no_text_layer` (never OCR'd), `ocr_no_text`
  or `ocr: <msg>`; `Store.scanned_documents` / `accepts_ocr` encode this. OCR text must
  never overwrite a `pdf` text layer. `sha256`/`n_bytes`/`n_pages` keep describing the
  upstream file. `documents` is raw, so OCR text survives derived reruns.
- `fetch-docs` OCRs new scans when ocrmypdf + tesseract are found (`--ocr` requires it,
  `--no-ocr` skips). Pipeline functions take the OCR callable as an argument; tests pass
  fakes and only `test_ocr_pdf_reads_a_real_scan` runs the real tool (skipped if absent).
- PDFs aren't stored, so `ocr` and `export-scans` re-download through `TestimonyClient`.

## Making common changes

- **New classifier rule / fix**: add a row to the parametrized table in
  `tests/test_classify.py` first, change `classify.py`, run `pytest`, then
  `mtestimony classify` and audit by rule (SQL below).
- **New org alias**: add `alias,canonical` to `data/org_aliases.csv`. The alias is
  the *output of `org_key()`* (lowercase, no punctuation), not the raw string.
- **New position pattern**: add a case to `tests/test_positions.py`; reuse the
  `_BILL` fragment; rerun `mtestimony positions` and spot-check evidence.
- **Schema change**: edit `SCHEMA` in `db.py`. It's `CREATE TABLE IF NOT EXISTS`
  with no migrations, so existing DBs won't pick up new columns. Add an
  `ALTER TABLE` or rebuild (raw tables need a re-crawl; derived ones just rerun).
- **Town list refresh**: `python scripts/build_reference.py` (Census Gazetteer).

Tunable thresholds: `crawl.settle_days=30`, `crawl.EARLIEST_SESSION=126`,
town fuzzy `score_cutoff=88` (single words, >=5 chars), org clustering
`token_sort_ratio>=92` (keys >=6 chars), form letters `fuzz.ratio>=90`,
`positions.HEAD_CHARS=3000`.

## Auditing results

```python
import sqlite3, pandas as pd

con = sqlite3.connect("data/testimony.sqlite")
pd.read_sql("SELECT category, rule, COUNT(*) n FROM submitters GROUP BY 1,2 ORDER BY n DESC", con)
pd.read_sql(
    """SELECT t.organization, COUNT(*) n FROM submitters s
               JOIN testimony t ON t.id = s.testimony_id
               WHERE s.rule = 'fallback_multiword' GROUP BY 1 ORDER BY n DESC LIMIT 50""",
    con,
)
pd.read_sql("SELECT position, method, COUNT(*) n FROM positions GROUP BY 1,2", con)
```

Measure accuracy on fresh samples, not the ones used to tune rules
(`mtestimony label-sample --seed N --out NEW.csv` / `mtestimony evaluate`). Round 1
(seed 13, `data/labels/submitters_round1_labeled.csv`) was used for tuning, so its
score is not a publishable accuracy. Round 2 (seed 2026) was drawn before the seed fix
and shares 299/300 rows with round 1, so it is not fresh either. `label-sample` refuses
to overwrite without `--force`. Pass every earlier label file as `--exclude FILE`
(repeatable) so a new round can't reuse labeled `testimony_id`s.

Positions: `mtestimony label-sample --kind positions --seed N` samples documents with
text, one per form-letter group, and hides the prediction; `evaluate` detects the file
by its `doc_url` column and re-runs the regexes on the stored text (needs `--db`).
With `--exclude`, a labeled document's whole form-letter group is skipped too.

Round 3 (drawn 2026-10-17, excludes every earlier file, zero overlap) is the first
publishable held-out set: `submitters_round3_todo.csv` (seed 31, n=300, 126th-132nd) and
`positions_round3_todo.csv` (seed 47, n=200, 132nd only). Labeled and scored 2026-10-22;
results are in README "Measuring accuracy". It is now SPENT for tuning, like rounds 1-2:
any rule or prompt change scored against it is no longer a held-out number, so a fix
needs a round 4 (`--exclude` all four earlier files) to report.

Labeling conventions settled in round 3: villages inside a town (Cundy's Harbor, Port
Clyde) are `town`; anything named "association" is `organization` even if its members are
officials (Maine Prosecutors Association), and so is a council of governments; Maine
Public and MECEP are `organization`.

## Known gaps / next steps

- Held-out accuracy (round 3, 2026-10-22). Submitters 95.3% (n=300); `government` recall
  0.79 is the weak class. Positions (n=200): regex 79.5%, precision when committed 95.1%,
  missed 33/196; with `--llm` 91.5%, 94.7%, missed 7/196. Round 1 gave regex precision
  99.3%, so tuning on it inflated that by ~4 points.
- Round-3 gaps (government bodies falling to `organization`; villages missing from the
  Census list) were fixed 2026-10-22 with rules `gov_commission`, `gov_board`,
  `gov_district`, `gov_dept_abbrev`, `town_village` (`data/village_towns.csv`) and
  `town_village_suffix`. The gain is UNMEASURED until a round-4 sample is labeled.
  Known misfires: "Maine Commission to End Domestic Violence" (a nonprofit) and
  "Maine Medical Center Dept of Family Medicine" come out `government`.
- LLM fallback, earlier check on `positions_round1_todo.csv` (n=196, 2026-10-16): 75.0% ->
  91.8%; the LLM's own commitments were 35/38 correct. Round 3 confirmed the gain. Both
  label sets are spent for prompt work.
- Scanned PDFs: ~2.2% overall (2,712 on 2026-10-22), ~3% of org/government letters.
  OCR added 2026-10-22 (ocrmypdf + external-tool import); the local backlog is going
  through FineReader. The Actions DB is separate and drains via `mtestimony ocr --limit 300`
  per run. OCR quality is unmeasured: no hand labels yet on OCR'd documents.
- Full historical crawl not done; the full PDF corpus is a day-plus at 1 req/sec,
  hence `--orgs-only` and the per-run `--limit` in `.github/workflows/scrape.yml`.

Full DB (126th-132nd, 4,083 hearings, 181,131 records, classified 2026-10-22): town 42.4%,
organization 37.3%, government 8.8%, legislator 7.7%, none 2.4%, unknown 1.1%,
place_other 0.3%.
