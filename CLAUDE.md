# CLAUDE.md

Scraper + dataset pipeline for written testimony submitted to Maine Legislature
committees (126th Legislature, 2013, to present). Python package `maine_testimony`,
CLI `mtestimony`. README.md is the human-facing overview; this file is the working
context for making changes.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                          # 110 tests, ~1s, all HTTP mocked
ruff check . && ruff format .      # line length 100, target py311

# Small live run (a few minutes at 1 req/sec)
mtestimony crawl -s 132 --per-committee 2
mtestimony classify
mtestimony fetch-docs --orgs-only --limit 100
mtestimony positions
mtestimony form-letters
mtestimony export                  # -> data/public/
mtestimony stats
```

Every command except `evaluate` takes `--db PATH` (default `data/testimony.sqlite`).
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
- Never redistribute PDF text; export links (`doc_url`) instead.
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
pd.read_sql("""SELECT t.organization, COUNT(*) n FROM submitters s
               JOIN testimony t ON t.id = s.testimony_id
               WHERE s.rule = 'fallback_multiword' GROUP BY 1 ORDER BY n DESC LIMIT 50""", con)
pd.read_sql("SELECT position, method, COUNT(*) n FROM positions GROUP BY 1,2", con)
```

Measure accuracy on fresh samples, not the ones used to tune rules
(`mtestimony label-sample --seed N --out NEW.csv` / `mtestimony evaluate`). Round 1
(seed 13, `data/labels/submitters_round1_labeled.csv`) was used for tuning, so its
score is not a publishable accuracy. `label-sample` refuses to overwrite without `--force`.

## Known gaps / next steps

- Hand-label 300 submitters + ~200 documents; report real precision/recall.
- LLM fallback for `unknown` positions (~20% of documents), validated on labels.
- Scanned PDFs (~1%) have no text layer; no OCR.
- Full historical crawl not done; the full PDF corpus is a day-plus at 1 req/sec,
  hence `--orgs-only` and the per-run `--limit` in `.github/workflows/scrape.yml`.

Last sample (132nd, 104 hearings, 6,264 records): town 52%, organization 34%,
government 7%, legislator 5%, unknown <1%. Positions on 250 PDFs: 80% labeled.
