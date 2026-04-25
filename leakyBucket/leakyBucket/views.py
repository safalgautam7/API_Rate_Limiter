from django.http import JsonResponse
from .decorators import rate_limit


def test_api(request):
    """
    Default test endpoint — uses the global middleware limits.
    Returns the current rate-limit state so you can observe it easily.
    """
    info = getattr(request, "_rate_limit_info", {})
    return JsonResponse({
        "message": "ok",
        "rate_limit": info,
    })


@rate_limit(
    burst={"capacity": 3, "refill_rate": 1},
    sustained={"capacity": 5, "refill_rate": 1},
)
def strict_api(request):
    """
    A tighter endpoint, uses the view-level @rate_limit decorator on top
    of the global middleware limits..
    """
    return JsonResponse({"message": "strict endpoint ok"})


def ping(request):
    """
    Lightweight health-check, still rate-limited by the middleware,
    but no custom decorator. 
    """
    return JsonResponse({"status": "pong"})