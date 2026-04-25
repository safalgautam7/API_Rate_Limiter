import functools
import logging

from django.http import JsonResponse

from .limiter import RedisTokenBucket
from .utils import get_rate_limit_key

logger = logging.getLogger(__name__)

_DEFAULT_BURST     = {"capacity": 5,  "refill_rate": 2}
_DEFAULT_SUSTAINED = {"capacity": 20, "refill_rate": 1}


def rate_limit(burst: dict | None = None, sustained: dict | None = None):
    """Per-view rate-limit decorator. Applies an independent bucket on top of the middleware."""
    burst_cfg     = burst or _DEFAULT_BURST
    sustained_cfg = sustained or _DEFAULT_SUSTAINED

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            key       = get_rate_limit_key(request)
            view_name = view_func.__name__

            burst_bucket = RedisTokenBucket(
                key=f"rl:decorator:{view_name}:burst:{key}",
                capacity=burst_cfg["capacity"],
                refill_rate=burst_cfg["refill_rate"],
            )
            sustained_bucket = RedisTokenBucket(
                key=f"rl:decorator:{view_name}:sustained:{key}",
                capacity=sustained_cfg["capacity"],
                refill_rate=sustained_cfg["refill_rate"],
            )

            allowed_burst,     burst_tokens     = burst_bucket.allow_request()
            allowed_sustained, sustained_tokens = sustained_bucket.allow_request()

            if not (allowed_burst and allowed_sustained):
                retry_after = 1 if not allowed_burst else int(1 / max(sustained_cfg["refill_rate"], 1))
                logger.info("Decorator rate limit exceeded: view=%s key=%s", view_name, key)
                return JsonResponse(
                    {"error": "Rate limit exceeded", "retry_after": retry_after, "policy": f"decorator:{view_name}"},
                    status=429,
                    headers={"Retry-After": str(retry_after)},
                )

            response = view_func(request, *args, **kwargs)
            response["X-RateLimit-View-Burst-Remaining"]     = int(burst_tokens)
            response["X-RateLimit-View-Sustained-Remaining"] = int(sustained_tokens)
            return response

        return wrapper
    return decorator
