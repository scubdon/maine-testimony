from datetime import UTC, date, datetime

import httpx
import pytest

from maine_testimony.client import TestimonyClient, UpstreamError
from maine_testimony.crawl import crawl


async def test_endpoints_parse(mock_api, client):
    sessions = await client.sessions()
    assert [s.session_number for s in sessions] == [132, 131]
    cmtes = await client.committees(132)
    assert {c.code for c in cmtes} == {"MAR", "TAX"}
    dates = await client.hearing_dates(132, "MAR")
    assert dates == [date(2025, 2, 6), date(2025, 3, 13)]
    recs = await client.testimony("MAR", date(2025, 2, 6))
    assert len(recs) == 4 and recs[0].organization == "Maine Lobstering Union"


async def test_retries_then_succeeds(mock_api, client, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = mock_api.get("getSessionNumbersDesc.asp")
    route.side_effect = [httpx.Response(503), httpx.Response(200, text="[]")]
    assert await client.sessions() == []
    assert route.call_count == 2


async def test_non_json_raises(mock_api, client):
    mock_api.get("getSessionNumbersDesc.asp").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    with pytest.raises(UpstreamError):
        await client.sessions()


async def test_disk_cache(mock_api, tmp_path):
    async with TestimonyClient(min_interval=0, cache_dir=tmp_path) as c:
        await c.testimony("MAR", date(2025, 2, 6))
        await c.testimony("MAR", date(2025, 2, 6))
        assert c.requests_made == 1
        await c.testimony("MAR", date(2025, 2, 6), refresh=True)
        assert c.requests_made == 2


async def test_crawl_is_incremental(mock_api, client, store):
    stats = await crawl(client, store, sessions=[132])
    assert stats.hearings_fetched == 3
    assert store.count("testimony") == 6
    assert store.count("hearings") == 3

    # Hearings from 2025 fetched "now" are settled, so a second run skips them.
    stats2 = await crawl(client, store, sessions=[132])
    assert stats2.hearings_fetched == 0
    assert stats2.hearings_skipped == 3


async def test_crawl_filters(mock_api, client, store):
    await crawl(client, store, sessions=[132], committees=["tax"])
    assert store.count("testimony") == 1
    store.conn.execute("DELETE FROM hearings")
    stats = await crawl(
        client, store, sessions=[132], since=date(2025, 3, 1), until=date(2025, 3, 31)
    )
    assert stats.hearings_fetched == 1


async def test_crawl_refetches_unsettled_hearing(mock_api, client, store):
    await crawl(client, store, sessions=[132], committees=["MAR"])
    # Pretend the March hearing was fetched the day after it happened.
    store.conn.execute(
        "UPDATE hearings SET fetched_at=? WHERE hearing_date='2025-03-13'",
        (datetime(2025, 3, 14, tzinfo=UTC).isoformat(),),
    )
    stats = await crawl(client, store, sessions=[132], committees=["MAR"])
    assert stats.hearings_fetched == 1 and stats.hearings_skipped == 1


async def test_per_committee_sampling(mock_api, client, store):
    stats = await crawl(client, store, sessions=[132], per_committee=1)
    assert stats.hearings_fetched == 2  # one MAR + one TAX


async def _no_sleep(_s):
    return None
