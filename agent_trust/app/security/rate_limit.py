import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from app.infra.settings import get_settings


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


@lru_cache(maxsize=1)
def get_rate_limiter() -> SlidingWindowRateLimiter:
    settings = get_settings()
    return SlidingWindowRateLimiter(settings.rate_limit_per_minute)