# app/services/cache_adapter.py
import json
import logging
import time
import uuid

from app.infra.redis_provider import get_redis_provider
from app.observability.metrics import (
    CACHE_ERRORS,
    CACHE_HITS,
    CACHE_INVALIDATIONS,
    CACHE_MISSES,
    CACHE_STALE_HITS,
)

logger = logging.getLogger(__name__)

CACHE_TTL = 300
STALE_TTL = 86400
STALE_PREFIX = "stale:"

LOCK_PREFIX = "lock:"
LOCK_TTL = 20

RELEASE_LUA = """
local val = redis.call("GET", KEYS[1])
if val == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class CacheAdapter:
    """
    Two-tier cache layer:
      - Primary (TTL=5min) : Normal fast cache used on every request.
      - Stale   (TTL=24h)  : Long-lived fallback served when the external DB
                              is down or its schema has changed.

    Neither tier will ever crash the application -- all Redis errors are silently
    swallowed and logged.
    """

    def __init__(self):
        self.redis = get_redis_provider().client
        self._release_script = None

    def _ensure_release_script(self):
        if self._release_script is None:
            self._release_script = self.redis.register_script(RELEASE_LUA)

    # ------------------------------------------------------------------ #
    # Primary cache                                                        #
    # ------------------------------------------------------------------ #

    def get(self, key: str) -> dict | None:
        try:
            data = self.redis.get(key)
            if data:
                CACHE_HITS.inc()
                return json.loads(data)
            CACHE_MISSES.inc()
            return None
        except Exception as exc:
            CACHE_ERRORS.inc()
            logger.warning("Cache GET failed for key '%s': %s", key, exc)
            return None

    def set(self, key: str, value: dict, ttl: int = CACHE_TTL) -> None:
        try:
            serialized = json.dumps(value, default=str)
            self.redis.setex(key, ttl, serialized)
            self.redis.setex(f"{STALE_PREFIX}{key}", STALE_TTL, serialized)
        except Exception as exc:
            logger.warning("Cache SET failed for key '%s': %s", key, exc)

    def invalidate(self, key: str) -> None:
        try:
            # One atomic DEL clears both the primary and the 24h stale copy. A
            # webhook declares the agent's state changed, so the stale fallback
            # would otherwise keep serving a superseded score during a DB outage.
            self.redis.delete(key, f"{STALE_PREFIX}{key}")
            CACHE_INVALIDATIONS.inc()
            logger.info("Cache manually invalidated for key '%s' (%s)", key, f"stale:{key}")
        except Exception as exc:
            CACHE_ERRORS.inc()
            logger.warning("Cache INVALIDATE failed for key '%s': %s", key, exc)

    # ------------------------------------------------------------------ #
    # Stale cache (fallback when DB is broken)                            #
    # ------------------------------------------------------------------ #

    def get_stale(self, key: str) -> dict | None:
        try:
            data = self.redis.get(f"{STALE_PREFIX}{key}")
            if data:
                CACHE_STALE_HITS.inc()
                logger.warning(
                    "Serving STALE cache for key '%s' -- DB may be unavailable or schema changed.",
                    key,
                )
                return json.loads(data)
            return None
        except Exception as exc:
            CACHE_ERRORS.inc()
            logger.warning("Stale cache GET failed for key '%s': %s", key, exc)
            return None

    # ------------------------------------------------------------------ #
    # Cache Lock (UUID-owned, Lua atomic release)                          #
    # ------------------------------------------------------------------ #

    def acquire_lock(self, key: str) -> str | None:
        token = str(uuid.uuid4())
        try:
            acquired = self.redis.set(f"{LOCK_PREFIX}{key}", token, nx=True, ex=LOCK_TTL)
            return token if acquired else None
        except Exception as exc:
            CACHE_ERRORS.inc()
            logger.error("Lock acquisition failed for '%s': %s", key, exc)
            return None

    def release_lock(self, key: str, token: str) -> None:
        try:
            self._ensure_release_script()
            self._release_script(keys=[f"{LOCK_PREFIX}{key}"], args=[token])
        except Exception as exc:
            logger.warning("Lock release failed for '%s': %s", key, exc)

    def wait_for_cache(
        self, key: str, max_retries: int = 6, initial_delay: float = 0.01
    ) -> dict | None:
        delay = initial_delay
        for _ in range(max_retries):
            time.sleep(delay)
            cached = self.get(key)
            if cached:
                return cached
            delay = min(delay * 2, 0.5)
        return None
