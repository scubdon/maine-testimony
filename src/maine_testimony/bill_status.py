"""Bill outcomes from the per-bill status page (``display_ps.asp?LD=&snum=``).

robots.txt disallows the bill *search* pages and LawMakerWeb, but not this page. Its
outcome fields are server-rendered HTML (no JSON endpoint), and the markup is loose
(unclosed ``<tr>``/``<td>``), so fields are pulled with narrow regexes keyed on the
page's own labels rather than a tree parser.

Three sections matter:

* ``sec0`` "Final Disposition" (+ "Governor's Action") and "Chaptered Law"
* ``sec3`` "Status In Committee": referral, latest committee action and report
* ``sec9`` "Divided Reports": one row per report with its signers (counted, not stored)
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .client import TestimonyClient, gather_limited

if TYPE_CHECKING:
    from .db import Store

log = logging.getLogger(__name__)

_DATE = r"[A-Z][a-z]{2} \d{1,2}, \d{4}"
_SECTION = re.compile(r'<div id="(sec\d+)"[^>]*>(.*?)(?=<div id="sec\d+"|</main>|\Z)', re.S | re.I)
_BILL_ID = re.compile(r"LD (\d+), ([HS]P) (\d+)")
_FINAL = re.compile(r"Final Disposition</span>(.*?)</p>", re.S)
_CHAPTER = re.compile(r"Chaptered Law</span>(.*?)</span>\s*</span>", re.S)
_INLINE = r"{label}</span>\s*<span class=\"inlineData\">(.*?)</span>"


@dataclass(frozen=True)
class DividedReport:
    label: str  # MAJ / MIN / A / B / C ...
    report: str  # "Ought To Pass As Amended"
    signers: int


@dataclass(frozen=True)
class BillStatus:
    ld: int | None = None
    paper_number: str | None = None  # "HP0165", the testimony table's format
    final_disposition: str | None = None  # "Enacted", "Died On Adjournment", ...
    final_date: date | None = None
    governor_action: str | None = None  # "Signed", "Veto", ...
    law_type: str | None = None  # ACTPUB / ACTPRV / RESLV ...
    chapter: int | None = None
    committee: str | None = None  # "Committee on Veterans and Legal Affairs"
    committee_action: str | None = None  # "Reported Out, Mar 12, 2025, OTP"
    committee_report: str | None = None  # "Ought To Pass" or "JUD, MAJ: ...; JUD, MIN: ..."
    committee_report_date: date | None = None
    divided_reports: tuple[DividedReport, ...] = field(default_factory=tuple)

    def divided_json(self) -> str | None:
        if not self.divided_reports:
            return None
        return json.dumps([asdict(r) for r in self.divided_reports])


class NotABillPage(ValueError):
    """The page has no "LD n, HP n" header (bad LD/session, or a maintenance page)."""


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def _date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s.strip().rstrip("."), "%b %d, %Y").date()


def _split_dated(s: str) -> tuple[str, date | None]:
    """``"Enacted, Jul 19, 2023"`` -> ``("Enacted", date(2023, 7, 19))``."""
    m = re.fullmatch(rf"(.*?),?\s*({_DATE})\.?", s.strip())
    if not m:
        return s.strip(), None
    return m.group(1).strip(), _date(m.group(2))


def _inline(section: str, label: str) -> str | None:
    m = re.search(_INLINE.format(label=re.escape(label)), section, re.S)
    return (_text(m.group(1)) or None) if m else None


def _divided(section: str) -> tuple[DividedReport, ...]:
    out = []
    for chunk in re.split(r'<tr class="newrow">', section)[1:]:
        m = re.search(r'<td valign="top">(.*?)</td>', chunk, re.S)
        if not m:
            continue
        label, _, report = _text(m.group(1)).partition(" - ")
        # Each signer is one bare <td>; the label/blank cells all carry valign.
        out.append(DividedReport(label.strip(), report.strip(), len(re.findall(r"<td>", chunk))))
    return tuple(out)


def parse_bill_page(page: str) -> BillStatus:
    sections = {m.group(1): m.group(2) for m in _SECTION.finditer(page)}
    sec0 = sections.get("sec0", "")
    bill = _BILL_ID.search(_text(sec0))
    if not bill:
        raise NotABillPage("no 'LD n, HP n' header")
    ld, chamber, num = int(bill.group(1)), bill.group(2), int(bill.group(3))

    disposition = final_date = governor = None
    if m := _FINAL.search(sec0):
        final, _, gov = _text(m.group(1)).partition("Governor's Action:")
        disposition, final_date = _split_dated(final)
        governor = _split_dated(gov)[0] if gov.strip() else None

    law_type = chapter = None
    m = _CHAPTER.search(sec0)
    if m and (c := re.search(r"([A-Z]+)\s*,?\s*Chapter\s+(\d+)", _text(m.group(1)))):
        law_type, chapter = c.group(1), int(c.group(2))

    sec3 = sections.get("sec3", "")
    report = _inline(sec3, "Latest Committee Report:")
    report_date = None
    # "Mar 12, 2025; Ought To Pass", or on some pages just "Mar 12, 2025" (no report text).
    if report and (r := re.fullmatch(rf"({_DATE})(?:;\s*(.*))?", report)):
        report_date, report = _date(r.group(1)), (r.group(2) or "").strip() or None

    return BillStatus(
        ld=ld,
        paper_number=f"{chamber}{num:04d}",
        final_disposition=disposition or None,
        final_date=final_date,
        governor_action=governor,
        law_type=law_type,
        chapter=chapter,
        committee=_inline(sec3, "Referred to"),
        committee_action=_inline(sec3, "Latest Committee Action:"),
        committee_report=report,
        committee_report_date=report_date,
        divided_reports=_divided(sections.get("sec9", "")),
    )


# ------------------------------------------------------------------- fetching
async def fetch_bill_statuses(
    client: TestimonyClient,
    store: Store,
    *,
    sessions: list[int] | None = None,
    limit: int | None = None,
    random: bool = False,
) -> Counter:
    """Fetch status pages for bills that have none yet, failed, or are still open.

    An open bill is fetched again (bypassing the disk cache) on every run until its
    page shows a final disposition.
    """
    stats: Counter = Counter()
    papers = _testimony_papers(store)

    async def one(session: int, ld: int, seen: bool) -> None:
        try:
            page = await client.bill_page(session, ld, refresh=seen)
        except Exception as e:
            store.save_bill_status(session, ld, None, error=f"download: {e}")
            stats["download_error"] += 1
            return
        _save_parsed(store, session, ld, page, papers, stats)

    queue = store.bills_needing_status(sessions, limit, random=random)
    await gather_limited([one(*q) for q in queue])
    return stats


def reparse_bill_pages(store: Store, cache_dir: Path) -> Counter:
    """Re-parse every cached status page (no network), e.g. after a parser fix."""
    stats: Counter = Counter()
    papers = _testimony_papers(store)
    for path in sorted((cache_dir / "display_ps").glob("*-*.html")):
        session, _, ld = path.stem.partition("-")
        _save_parsed(store, int(session), int(ld), path.read_text("utf-8"), papers, stats)
    return stats


def _testimony_papers(store: Store) -> dict[tuple[int, int], set[str]]:
    out: dict[tuple[int, int], set[str]] = {}
    for s, ld, paper in store.conn.execute(
        "SELECT DISTINCT session_number, ld, paper_number FROM testimony WHERE ld IS NOT NULL"
    ):
        out.setdefault((s, ld), set()).add(paper)
    return out


def _save_parsed(
    store: Store,
    session: int,
    ld: int,
    page: str,
    papers: dict[tuple[int, int], set[str]],
    stats: Counter,
) -> None:
    try:
        status = parse_bill_page(page)
    except NotABillPage:
        store.save_bill_status(session, ld, None, error="not_bill_page")
        stats["not_bill_page"] += 1
        return
    if status.ld != ld or status.paper_number not in papers.get((session, ld), set()):
        # The join key is (session, LD); a different paper means the LD was reused or
        # the testimony row is mislabeled. Keep the row, but count it for auditing.
        log.warning(
            "%s LD %s: page says %s, testimony says %s",
            session,
            ld,
            status.paper_number,
            papers.get((session, ld)),
        )
        stats["paper_mismatch"] += 1
    store.save_bill_status(session, ld, status)
    stats["final" if status.final_disposition else "open"] += 1
