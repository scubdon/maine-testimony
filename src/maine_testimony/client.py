"""Polite async client for the Maine Legislature testimony JSON endpoints.

The public testimony pages at https://legislature.maine.gov/bills/testimony.html
are a thin jQuery shell over a handful of undocumented GET endpoints that return
JSON (served as ``text/javascript``). This module wraps them with:

* a global minimum interval between requests (default 1 req/sec),
* bounded concurrency,
* retries with exponential backoff on 5xx / network errors,
* an optional on-disk cache of raw responses, so re-processing never needs the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .models import Committee, HearingDate, Session, TestimonyRecord

log = logging.getLogger(__name__)

BASE_URL = "https://legislature.maine.gov/bills/"
DOC_URL = BASE_URL + "getTestimonyDoc.asp?id={id}"
CONTACT = os.environ.get("MTESTIMONY_CONTACT", "https://github.com/scubdon/maine-testimony")
USER_AGENT = f"maine-testimony/0.1 (research dataset; +{CONTACT})"


class UpstreamError(RuntimeError):
    """The server returned something that is not the JSON we expect."""


class RateLimiter:
    """Enforce a minimum interval between request *starts* across all tasks."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class TestimonyClient:
    __test__ = False

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        min_interval: float = 1.0,
        concurrency: int = 2,
        max_retries: int = 4,
        cache_dir: Path | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/javascript"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = RateLimiter(min_interval)
        self._sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.cache_dir = cache_dir
        self.requests_made = 0

    async def __aenter__(self) -> TestimonyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- low level
    def _cache_path(self, endpoint: str, params: dict[str, Any]) -> Path | None:
        if not self.cache_dir:
            return None
        key = endpoint + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache_dir / endpoint.removesuffix(".asp") / f"{digest}.json"

    async def _get(self, endpoint: str, params: dict[str, Any]) -> httpx.Response:
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            async with self._sem:
                await self._limiter.wait()
                try:
                    self.requests_made += 1
                    resp = await self._http.get(endpoint, params=params)
                except httpx.TransportError as e:
                    err: Exception = e
                else:
                    if resp.status_code < 500 and resp.status_code != 429:
                        resp.raise_for_status()
                        return resp
                    err = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp
                    )
            if attempt == self.max_retries:
                raise err
            log.warning("GET %s %s failed (%s); retrying in %.0fs", endpoint, params, err, backoff)
            await asyncio.sleep(backoff)
            backoff *= 2
        raise AssertionError("unreachable")

    async def get_json(
        self, endpoint: str, params: dict[str, Any] | None = None, *, refresh: bool = False
    ) -> Any:
        params = params or {}
        path = self._cache_path(endpoint, params)
        if path and path.exists() and not refresh:
            return json.loads(path.read_text())
        resp = await self._get(endpoint, params)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise UpstreamError(f"{endpoint} {params}: non-JSON body {resp.text[:120]!r}") from e
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False))
        return data

    # --------------------------------------------------------------- endpoints
    async def sessions(self) -> list[Session]:
        rows = await self.get_json("getSessionNumbersDesc.asp", refresh=True)
        return [Session.model_validate(r) for r in rows]

    async def committees(self, session: int, *, refresh: bool = True) -> list[Committee]:
        rows = await self.get_json("getTestyCmte.asp", {"snum": session}, refresh=refresh)
        return [Committee.model_validate(r) for r in rows]

    async def hearing_dates(
        self, session: int, committee_code: str, *, refresh: bool = True
    ) -> list[date]:
        rows = await self.get_json(
            "getTestyCmteDates.asp", {"snum": session, "comCode": committee_code}, refresh=refresh
        )
        return sorted({HearingDate.model_validate(r).presented_date for r in rows})

    async def testimony(
        self, committee_code: str, hearing_date: date, *, refresh: bool = False
    ) -> list[TestimonyRecord]:
        rows = await self.get_json(
            "getTestyForCmteHearingDate.asp",
            {"hearingDate": hearing_date.isoformat(), "comCode": committee_code},
            refresh=refresh,
        )
        return [TestimonyRecord.model_validate(r) for r in rows]

    async def document(self, testimony_id: int) -> bytes:
        resp = await self._get("getTestimonyDoc.asp", {"id": testimony_id})
        return resp.content

    def bill_page_cache_path(self, session: int, ld: int) -> Path | None:
        return self.cache_dir / "display_ps" / f"{session}-{ld}.html" if self.cache_dir else None

    async def bill_page(self, session: int, ld: int, *, refresh: bool = False) -> str:
        """The bill's status page (HTML). Cached by readable name so ``fetch-status
        --reparse`` can re-read every page offline after a parser change."""
        path = self.bill_page_cache_path(session, ld)
        if path and path.exists() and not refresh:
            return path.read_text(encoding="utf-8")
        resp = await self._get("display_ps.asp", {"LD": ld, "snum": session})
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(resp.text, encoding="utf-8")
        return resp.text


async def gather_limited(coros: Sequence, limit: int = 8) -> list:
    """Run coroutines with a cap on how many are scheduled at once (the client
    itself also rate-limits, this just keeps memory bounded)."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))
