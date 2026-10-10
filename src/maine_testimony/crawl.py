"""Crawl orchestration: sessions -> committees -> hearing dates -> testimony records.

Incremental by design. A hearing that was fetched more than ``settle_days`` after it
happened is treated as final and skipped on later runs; more recent hearings are
re-fetched because written testimony keeps arriving for a while after the hearing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .client import TestimonyClient, gather_limited
from .db import Store

log = logging.getLogger(__name__)

# Testimony is generally available online from the 126th Legislature (2013-14) on.
EARLIEST_SESSION = 126


@dataclass
class CrawlStats:
    hearings_seen: int = 0
    hearings_fetched: int = 0
    hearings_skipped: int = 0
    records: int = 0
    errors: list[str] = field(default_factory=list)


async def crawl(
    client: TestimonyClient,
    store: Store,
    sessions: list[int] | None = None,
    committees: list[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    settle_days: int = 30,
    max_hearings: int | None = None,
    per_committee: int | None = None,
) -> CrawlStats:
    """Crawl testimony listings.

    ``max_hearings`` caps the whole run; ``per_committee`` takes an evenly spaced
    sample of at most N hearings from each committee (handy for quick audits).
    """
    stats = CrawlStats()
    all_sessions = await client.sessions()
    store.upsert_sessions(all_sessions)
    if sessions is None:
        sessions = [s.session_number for s in all_sessions if s.session_number >= EARLIEST_SESSION]

    wanted = {c.upper() for c in committees} if committees else None
    now = datetime.now(UTC)
    budget = max_hearings

    for snum in sorted(sessions, reverse=True):
        cmtes = await client.committees(snum)
        store.upsert_committees(snum, cmtes)
        for cmte in cmtes:
            if wanted and cmte.code.upper() not in wanted:
                continue
            dates = await client.hearing_dates(snum, cmte.code)
            todo: list[date] = []
            for d in dates:
                if (since and d < since) or (until and d > until):
                    continue
                stats.hearings_seen += 1
                fetched = store.hearing_fetched_at(snum, cmte.code, d)
                if fetched and (fetched.date() - d).days > settle_days:
                    stats.hearings_skipped += 1
                    continue
                todo.append(d)
            if per_committee and len(todo) > per_committee:
                step = len(todo) / per_committee
                todo = [todo[int(i * step)] for i in range(per_committee)]
            if budget is not None:
                todo, budget = todo[:budget], max(0, budget - len(todo))

            async def fetch(d: date, code: str = cmte.code, s: int = snum) -> None:
                # Re-pull (bypass raw cache) anything not yet settled.
                recent = (now.date() - d).days <= settle_days
                try:
                    recs = await client.testimony(code, d, refresh=recent)
                except Exception as e:  # keep crawling; report at the end
                    stats.errors.append(f"{s}/{code}/{d}: {e}")
                    log.error("failed %s %s %s: %s", s, code, d, e)
                    return
                store.save_hearing(s, code, d, recs)
                stats.hearings_fetched += 1
                stats.records += len(recs)

            await gather_limited([fetch(d) for d in todo])
            if todo:
                log.info("%s %s: %d hearings", snum, cmte.code, len(todo))
            if budget == 0:
                return stats
    return stats
