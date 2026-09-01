from __future__ import annotations

import fakeredis
from xiosync.core.rate_limit import RateLimiter


def test_rate_limit_shared_across_replicas() -> None:
    shared = fakeredis.FakeRedis()
    replica_a = RateLimiter(shared)
    replica_b = RateLimiter(shared)
    for _ in range(3):
        assert replica_a.check("replica-test", 5, 10).allowed
    assert replica_b.check("replica-test", 5, 10).remaining == 1
    assert replica_b.check("replica-test", 5, 10).allowed is True
    assert replica_b.check("replica-test", 5, 10).allowed is False
