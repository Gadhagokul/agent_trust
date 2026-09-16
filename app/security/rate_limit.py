# app\security\rate_limit.py
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from app.domain.errors import DatabaseUnavailableError
from app.infra.redis_provider import get_redis_provider
from app.infra.settings import get_settings

SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local boundary = now - window

redis.call("ZREMRANGEBYSCORE", key, "-inf", boundary)
local count = redis.call("ZCOUNT", key, boundary, now)

if count < limit then
    redis.call("ZADD", key, now, now)
    redis.call("EXPIRE", key, window)
    return 1
else
    return 0
end
"""


class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int):
        self._limit = limit_per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        window_start = now - 60

        with self._lock:
            dq = self._events[key]

            while dq and dq[0] < window_start:
                dq.popleft()

            if len(dq) >= self._limit:
                return False

            dq.append(now)
            return True


class RedisSlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int):
        self._limit = limit_per_minute
        self._window = 60
        self._lua = None
        self._redis_provider = None

    def _ensure_script(self):
        if self._lua is None:
            self._redis_provider = get_redis_provider()
            self._lua = self._redis_provider.client.register_script(SLIDING_WINDOW_LUA)

    def allow(self, key: str) -> bool:
        self._ensure_script()
        assert self._lua is not None
        try:
            result = self._lua(
                keys=[key],
                args=[self._limit, self._window, time.time()],
            )
            return bool(result)
        except Exception as exc:
            raise DatabaseUnavailableError(f"Rate limiter unavailable: {exc}") from exc


@lru_cache(maxsize=1)
def get_rate_limiter():
    settings = get_settings()
    if settings.rate_limiter_backend == "redis":
        return RedisSlidingWindowRateLimiter(settings.rate_limit_per_minute)
    return SlidingWindowRateLimiter(settings.rate_limit_per_minute)
