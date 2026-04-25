import time
import logging

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

# Shared pool and registered Lua script — created once per process.
_pool = redis.ConnectionPool.from_url(settings.REDIS_URL, decode_responses=False)
_redis_client = redis.Redis(connection_pool=_pool)

with open(settings.BASE_DIR / "lua" / "token_bucket.lua") as f:
    _lua_script = _redis_client.register_script(f.read())


class RedisTokenBucket:
    def __init__(self, key: str, capacity: int, refill_rate: int):
        self.key = key
        self.capacity = capacity
        self.refill_rate = refill_rate

    def allow_request(self) -> tuple[bool, float]:
        """Consume one token. Returns (allowed, remaining_tokens). Fails open on Redis errors."""
        now = int(time.time())
        try:
            result = _lua_script(
                keys=[self.key],
                args=[self.capacity, self.refill_rate, now],
            )
            return bool(result[0]), float(result[1])
        except redis.RedisError as exc:
            logger.warning("Rate limiter Redis error for key '%s': %s", self.key, exc)
            return True, float(self.capacity)