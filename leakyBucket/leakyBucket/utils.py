def get_client_ip(request) -> str:
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def get_rate_limit_key(request) -> str:
    """Return a per-user key for authenticated requests, per-IP otherwise."""
    if request.user.is_authenticated:
        return f"user:{request.user.id}"
    return f"ip:{get_client_ip(request)}"


def get_api_scope(request) -> str:
    """Return the request path without the leading slash."""
    return request.path.lstrip("/")