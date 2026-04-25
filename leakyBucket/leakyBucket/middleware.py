import logging

from django.conf import settings
from django.http import JsonResponse

from .limiter import RedisTokenBucket
from .utils import get_rate_limit_key, get_api_scope

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Token-bucket rate limiter. Rules come from settings.RATE_LIMIT_RULES."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "RATE_LIMIT_ENABLED", True)
        self.rules = getattr(settings, "RATE_LIMIT_RULES", {
            "default": {
                "burst":     {"capacity": 10, "refill_rate": 5},
                "sustained": {"capacity": 100, "refill_rate": 1},
            }
        })

    def get_rule(self, path: str) -> tuple[dict, str]:
        """Longest-prefix match against RATE_LIMIT_RULES. Falls back to 'default'."""
        best_prefix = ""
        best_rule = self.rules["default"]

        for prefix, rule in self.rules.items():
            if prefix == "default":
                continue
            if path.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_rule = rule

        return best_rule, (best_prefix or "default")


    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        scope = get_api_scope(request)
        if scope.startswith("admin/"):
            return self.get_response(request)

        key = get_rate_limit_key(request)
        rule, matched_prefix = self.get_rule(scope)

        burst_bucket = RedisTokenBucket(
            key=f"rl:burst:{matched_prefix}:{key}",
            capacity=rule["burst"]["capacity"],
            refill_rate=rule["burst"]["refill_rate"],
        )
        sustained_bucket = RedisTokenBucket(
            key=f"rl:sustained:{matched_prefix}:{key}",
            capacity=rule["sustained"]["capacity"],
            refill_rate=rule["sustained"]["refill_rate"],
        )

        allowed_burst,     burst_tokens     = burst_bucket.allow_request()
        allowed_sustained, sustained_tokens = sustained_bucket.allow_request()

        if not (allowed_burst and allowed_sustained):
            retry_after = (
                1 if not allowed_burst
                else int(1 / max(rule["sustained"]["refill_rate"], 1))
            )
            logger.info(
                "Rate limit exceeded: key=%s scope=%s policy=%s",
                key, scope, matched_prefix,
            )
            return JsonResponse(
                {
                    "error": "Rate limit exceeded",
                    "retry_after": retry_after,
                    "policy": matched_prefix,
                },
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

        request._rate_limit_info = {
            "policy":             matched_prefix,
            "burst_limit":        rule["burst"]["capacity"],
            "burst_remaining":    int(burst_tokens),
            "sustained_limit":    rule["sustained"]["capacity"],
            "sustained_remaining": int(sustained_tokens),
        }

        response = self.get_response(request)

        response["X-RateLimit-Policy"]             = matched_prefix
        response["X-RateLimit-Burst-Limit"]        = rule["burst"]["capacity"]
        response["X-RateLimit-Burst-Remaining"]    = int(burst_tokens)
        response["X-RateLimit-Sustained-Limit"]    = rule["sustained"]["capacity"]
        response["X-RateLimit-Sustained-Remaining"] = int(sustained_tokens)

        return response