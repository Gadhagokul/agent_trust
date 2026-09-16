from functools import lru_cache

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from app.infra.settings import get_settings


class RedisProvider:
    def __init__(self):
        settings = get_settings()
        self.client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
            retry_on_timeout=True,
            retry=Retry(ExponentialBackoff(cap=10, base=1), 3),
        )

    def ping(self) -> bool:
        try:
            return self.client.ping()
        except redis.RedisError:
            return False


@lru_cache(maxsize=1)
def get_redis_provider() -> RedisProvider:
    return RedisProvider()
