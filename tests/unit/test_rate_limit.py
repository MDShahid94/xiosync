from __future__ import annotations

import fakeredis
import pytest
from xiosync.core.rate_limit import RateLimiter, RateLimiterNotAvailable


def test_allows_under_limit_and_reports_remaining() -> None:
    limiter = RateLimiter(fakeredis.FakeRedis())
    result = limiter.check("other", 3, 10)
    assert result.allowed is True
    assert result.remaining == 2


def test_blocks_at_exact_limit() -> None:
    limiter = RateLimiter(fakeredis.FakeRedis())
    for _ in range(2):
        assert limiter.check("key", 2, 10).allowed
    result = limiter.check("key", 2, 10)
    assert result.allowed is False
    assert result.remaining == 0


def test_unavailable_store_raises() -> None:
    class BrokenRedis:
        def pipeline(self, **_: object) -> object:
            raise ConnectionError("down")

    with pytest.raises(RateLimiterNotAvailable):
        RateLimiter(BrokenRedis()).check("key", 1, 10)  # type: ignore[arg-type]
