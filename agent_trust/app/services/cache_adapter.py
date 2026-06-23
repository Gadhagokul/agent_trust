#app\services\cache_adapter.py
import json
import logging
import time

from app.infra.redis_provider import get_redis_provider

logger = logging.getLogger(__name__)

# How long a fresh result is cached (5 minutes)
CACHE_TTL = 300
# How long a stale result is kept as fallback (24 hours)
STALE_TTL = 86400
STALE_PREFIX = "stale:"

LOCK_PREFIX = "lock:"          
LOCK_TTL = 20  


class CacheAdapter:
    """
    Two-tier cache layer:
      - Primary (TTL=5min) : Normal fast cache used on every request.
      - Stale   (TTL=24h)  : Long-lived fallback served when the external DB
                              is down or its schema has changed.

    Neither tier will ever crash the application — all Redis errors are silently
    swallowed and logged.
    """

    def __init__(self):
        self.redis = get_redis_provider().client

    # ------------------------------------------------------------------ #
    # Primary cache                                                        #
    # ------------------------------------------------------------------ #

    def get(self, key: str) -> dict | None:
        """Return fresh cached result, or None if miss/expired."""
        try:
            data = self.redis.get(key)
            return json.loads(data) if data else None
        except Exception as exc:
            logger.warning("Cache GET failed for key '%s': %s", key, exc)
            return None

    def set(self, key: str, value: dict, ttl: int = CACHE_TTL) -> None:
        """Write to primary cache and refresh the stale copy at the same time."""
        try:
            serialized = json.dumps(value, default=str)
            self.redis.setex(key, ttl, serialized)
            # Always refresh stale copy whenever we have fresh data
            self.redis.setex(f"{STALE_PREFIX}{key}", STALE_TTL, serialized)
        except Exception as exc:
            logger.warning("Cache SET failed for key '%s': %s", key, exc)

    def invalidate(self, key: str) -> None:
        """Manually invalidate the primary cache (keeps stale as fallback)."""
        try:
            self.redis.delete(key)
            logger.info("Cache manually invalidated for key '%s'", key)
        except Exception as exc:
            logger.warning("Cache INVALIDATE failed for key '%s': %s", key, exc)

    # ------------------------------------------------------------------ #
    # Stale cache (fallback when DB is broken)                            #
    # ------------------------------------------------------------------ #

    def get_stale(self, key: str) -> dict | None:
        """
        Return the last known good result even if the primary cache has expired.
        Used as a fallback when the DB is down or schema has changed.
        """
        try:
            data = self.redis.get(f"{STALE_PREFIX}{key}")
            if data:
                logger.warning(
                    "Serving STALE cache for key '%s' — DB may be unavailable or schema changed.",
                    key,
                )
                return json.loads(data)
            return None
        except Exception as exc:
            logger.warning("Stale cache GET failed for key '%s': %s", key, exc)
            return None
        

    # Cache Lock Methods
    # ===========================

    def acquire_lock(self, key: str) -> bool:
        """Prevent cache stampede using Redis NX lock"""
        try:
            return self.redis.set(f"{LOCK_PREFIX}{key}", "1", nx=True, ex=LOCK_TTL)
        except Exception as exc:
            logger.warning("Lock failed for '%s' → proceeding without protection: %s", key, exc)
            return True  # fail-open

    def release_lock(self, key: str) -> None:
        try:
            self.redis.delete(f"{LOCK_PREFIX}{key}")
        except Exception:
            pass

    def wait_for_cache(self, key: str, retries: int = 10, delay: float = 0.05) -> dict | None:
        """Wait briefly for another request to populate cache"""
        for _ in range(retries):
            time.sleep(delay)
            cached = self.get(key)
            if cached:
                return cached
        return None