# How to Build This Rate Limiter From Scratch

This document explains every concept and every line of code in this project so you can rebuild it independently.

---

## The Big Picture

A **rate limiter** controls how many requests a client can make in a given time window.
This one uses the **Token Bucket** algorithm, stores state in **Redis**, and runs the algorithm atomically via a **Lua script**.

The Django side is a **middleware** — code that intercepts every HTTP request before it reaches any view.

```
Client
  │
  ▼
Django Middleware  ← checks Redis
  │
  ├─ too many requests → return 429 immediately
  │
  └─ OK → pass to view → return response + rate-limit headers
```

---

## The Token Bucket Algorithm

Think of each client as having a bucket.

- The bucket has a maximum capacity (e.g. 10 tokens).
- Every request takes out 1 token.
- Tokens are added back at a fixed rate per second (e.g. 5/s).
- If the bucket is empty, the request is blocked.

This project actually uses **two buckets per client**:

| Bucket | Purpose | Example |
|---|---|---|
| **Burst** | Allows a short spike of fast requests | capacity=10, refill=5/s |
| **Sustained** | Caps the long-term average | capacity=100, refill=1/s |

Both buckets must have at least 1 token for a request to go through. This lets clients make bursts but prevents sustained abuse.

---

## Why Redis?

Rate-limit state (token count, last refill time) needs to be:
1. **Shared** across all Django processes/workers (a single Python dict won't work)
2. **Fast** (checked on every single request)
3. **Atomic** (two workers checking at the same time must not both see "1 token left" and both allow the request)

Redis solves 1 and 2. Problem 3 is solved with a Lua script.

---

## Why a Lua Script?

If you do the token-bucket logic in Python:

```python
# DANGER: race condition here!
tokens = redis.get(key)          # worker A reads 1 token
tokens = redis.get(key)          # worker B also reads 1 token
redis.set(key, tokens - 1)       # worker A sets to 0 — allowed
redis.set(key, tokens - 1)       # worker B sets to 0 — also allowed!
```

Both workers allowed a request even though there was only 1 token.

Redis executes Lua scripts **atomically** — the entire script runs as a single uninterruptible unit. No other command can sneak in between steps.

---

## File-by-File Walkthrough

### `lua/token_bucket.lua`

This is the core algorithm. It runs entirely inside Redis.

```lua
local key = KEYS[1]                         -- e.g. "rl:burst:default:ip:127.0.0.1"
local capacity    = tonumber(ARGV[1])       -- max tokens
local refill_rate = tonumber(ARGV[2])       -- tokens added per second
local now         = tonumber(ARGV[3])       -- current Unix timestamp (sent from Python)

-- Read the current state of this bucket from Redis
local data       = redis.call("HMGET", key, "tokens", "last_refill")
local tokens     = tonumber(data[1])
local last_refill = tonumber(data[2])

-- First time this key is seen: initialise with a full bucket
if tokens == nil then
    tokens     = capacity
    last_refill = now
end

-- Refill: add tokens proportional to time elapsed since last check
-- math.min makes sure we never exceed capacity
local elapsed = now - last_refill
tokens = math.min(capacity, tokens + (elapsed * refill_rate))

-- Try to consume 1 token
local allowed = 0
if tokens >= 1 then
    tokens  = tokens - 1
    allowed = 1
end

-- Write updated state back and set a 1-hour expiry so unused keys clean up
redis.call("HMSET", key, "tokens", tokens, "last_refill", now)
redis.call("EXPIRE", key, 3600)

return {allowed, tokens}    -- Python reads these two values
```

**Key design point:** `HMGET`/`HMSET` store a small hash (two fields) per bucket key. This is more efficient than two separate `GET`/`SET` calls and keeps both fields in sync atomically.

---

### `leakyBucket/limiter.py`

This is the Python wrapper around the Lua script.

```python
# A ConnectionPool keeps a set of persistent TCP sockets to Redis.
# Without this, a new connection would be opened on every request.
_pool = redis.ConnectionPool.from_url(settings.REDIS_URL, decode_responses=False)
_redis_client = redis.Redis(connection_pool=_pool)

# register_script() uploads the Lua code to Redis once and returns a callable.
# We do this at module load time (once per process), not per-request.
with open(settings.BASE_DIR / "lua" / "token_bucket.lua") as f:
    _lua_script = _redis_client.register_script(f.read())
```

Why `decode_responses=False`? The Lua script returns raw bytes. If `decode_responses=True`, the redis client tries to decode them as UTF-8 strings, which breaks `float(result[1])`.

```python
class RedisTokenBucket:
    def __init__(self, key: str, capacity: int, refill_rate: int):
        self.key = key              # unique Redis key for this bucket
        self.capacity = capacity
        self.refill_rate = refill_rate

    def allow_request(self) -> tuple[bool, float]:
        now = int(time.time())      # Unix timestamp in whole seconds
        try:
            result = _lua_script(
                keys=[self.key],                              # KEYS[1] in Lua
                args=[self.capacity, self.refill_rate, now], # ARGV[1..3] in Lua
            )
            return bool(result[0]), float(result[1])    # (allowed, remaining)
        except redis.RedisError as exc:
            # Fail-open: if Redis is down, let the request through.
            # This is a deliberate availability > strictness trade-off.
            logger.warning("Rate limiter Redis error for key '%s': %s", self.key, exc)
            return True, float(self.capacity)
```

---

### `leakyBucket/utils.py`

Three small helpers used by the middleware and decorator.

```python
def get_client_ip(request) -> str:
    # X-Forwarded-For is set by load balancers/proxies.
    # It can contain a chain like "client, proxy1, proxy2" — we want the first.
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")

def get_rate_limit_key(request) -> str:
    # Authenticated users get their own bucket so they're not affected by
    # other users behind the same NAT/shared IP.
    if request.user.is_authenticated:
        return f"user:{request.user.id}"
    return f"ip:{get_client_ip(request)}"

def get_api_scope(request) -> str:
    # "/api/auth/login/" → "api/auth/login/"
    # Used for rule matching. The leading slash is stripped.
    return request.path.lstrip("/")
```

---

### `leakyBucket/middleware.py`

Django middleware is a class with `__init__` and `__call__`. Django calls `__init__` once at startup and `__call__` on every request.

```python
class RateLimitMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response   # the next layer in the middleware chain

        # Read settings once at startup. getattr() provides defaults so the
        # middleware doesn't crash if someone forgets to add these to settings.py.
        self.enabled = getattr(settings, "RATE_LIMIT_ENABLED", True)
        self.rules   = getattr(settings, "RATE_LIMIT_RULES", { "default": {...} })
```

**Longest-prefix matching:**

```python
def get_rule(self, path: str) -> tuple[dict, str]:
    best_prefix = ""
    best_rule   = self.rules["default"]

    for prefix, rule in self.rules.items():
        if prefix == "default":
            continue
        # A longer matching prefix is more specific, so it wins.
        if path.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_rule   = rule

    return best_rule, (best_prefix or "default")
```

Example with two rules `"api/"` and `"api/auth/"`:
- Path `"api/auth/login/"` → both prefixes match, `"api/auth/"` is longer → wins
- Path `"api/posts/"` → only `"api/"` matches → uses that rule
- Path `"health/"` → nothing matches → falls back to `"default"`

**The main request handler:**

```python
def __call__(self, request):
    if not self.enabled:
        return self.get_response(request)   # bypass entirely

    scope = get_api_scope(request)
    if scope.startswith("admin/"):
        return self.get_response(request)   # never rate-limit the admin

    key  = get_rate_limit_key(request)      # "user:42" or "ip:1.2.3.4"
    rule, matched_prefix = self.get_rule(scope)

    # Each bucket gets a unique Redis key so burst and sustained are independent.
    # Key format: "rl:<type>:<policy>:<client_key>"
    burst_bucket = RedisTokenBucket(
        key=f"rl:burst:{matched_prefix}:{key}", ...)
    sustained_bucket = RedisTokenBucket(
        key=f"rl:sustained:{matched_prefix}:{key}", ...)

    allowed_burst,     burst_tokens     = burst_bucket.allow_request()
    allowed_sustained, sustained_tokens = sustained_bucket.allow_request()

    if not (allowed_burst and allowed_sustained):
        # Retry-After tells the client how many seconds to wait.
        retry_after = 1 if not allowed_burst else int(1 / refill_rate)
        return JsonResponse({...}, status=429, headers={"Retry-After": ...})

    # Attach info to request object so views can read it (optional).
    request._rate_limit_info = { "policy": ..., "burst_remaining": ..., ... }

    response = self.get_response(request)   # call the actual view

    # Tell the client about their remaining quota via standard headers.
    response["X-RateLimit-Burst-Limit"]     = rule["burst"]["capacity"]
    response["X-RateLimit-Burst-Remaining"] = int(burst_tokens)
    # ... etc
    return response
```

---

### `leakyBucket/decorators.py`

The decorator pattern here is a **factory**: calling `rate_limit(...)` returns a decorator, which wraps a view function.

```python
def rate_limit(burst=None, sustained=None):   # ← you call this
    burst_cfg     = burst or _DEFAULT_BURST
    sustained_cfg = sustained or _DEFAULT_SUSTAINED

    def decorator(view_func):                 # ← this wraps the view
        @functools.wraps(view_func)           # preserves __name__, __doc__ etc.
        def wrapper(request, *args, **kwargs):
            # Same logic as middleware but keyed to the specific view name.
            # Key: "rl:decorator:<view_name>:burst:<client_key>"
            ...
            if not allowed:
                return JsonResponse({...}, status=429)
            response = view_func(request, *args, **kwargs)  # call the real view
            response["X-RateLimit-View-Burst-Remaining"] = int(burst_tokens)
            return response

        return wrapper
    return decorator
```

Usage:
```python
@rate_limit(burst={"capacity": 3, "refill_rate": 1})
def my_view(request):
    ...
```

The decorator creates its own Redis buckets completely separate from the middleware's buckets, so both checks apply independently.

---

### `leakyBucket/settings.py` — rate-limit section

```python
RATE_LIMIT_ENABLED = True   # flip to False to disable globally (e.g. in tests)

RATE_LIMIT_RULES = {
    "default": {
        "burst":     {"capacity": 10, "refill_rate": 5},
        "sustained": {"capacity": 100, "refill_rate": 1},
    },
    # To add a new API just uncomment and adjust:
    # "api/auth/": {
    #     "burst":     {"capacity": 5, "refill_rate": 2},
    #     "sustained": {"capacity": 30, "refill_rate": 1},
    # },
}
```

- **capacity** — max requests the client can fire before hitting the limit
- **refill_rate** — tokens per second added back to the bucket
- With `capacity=10, refill_rate=5`: a burst of 10 is allowed, then at most 5 requests/second forever

---

### `leakyBucket/views.py`

```python
def test_api(request):
    # _rate_limit_info is attached by the middleware (only present if allowed).
    info = getattr(request, "_rate_limit_info", {})
    return JsonResponse({"message": "ok", "rate_limit": info})

@rate_limit(burst={"capacity": 3, "refill_rate": 1},
            sustained={"capacity": 5, "refill_rate": 1})
def strict_api(request):
    # This view has its own tight bucket on top of the global middleware.
    return JsonResponse({"message": "strict endpoint ok"})

def ping(request):
    # A plain view — no decorator. Rate-limited only by the middleware.
    return JsonResponse({"status": "pong"})
```

---

## How to Add a New API

1. **Write a view** in `views.py` — just a normal Django view function.
2. **Register the URL** in `urls.py`:
   ```python
   path("api/newfeature/", my_view, name="my_view"),
   ```
3. *(Optional)* **Add a rule** in `settings.RATE_LIMIT_RULES` if you want limits different from the default:
   ```python
   "api/newfeature/": {
       "burst":     {"capacity": 5, "refill_rate": 2},
       "sustained": {"capacity": 20, "refill_rate": 1},
   },
   ```

That's all. No changes to middleware, limiter, or Lua script.

---

## Redis Key Structure

Every bucket is stored in Redis under a structured key so they don't collide:

```
rl : <type>      : <policy>     : <client>
rl : burst       : default      : ip:127.0.0.1
rl : sustained   : default      : ip:127.0.0.1
rl : burst       : api/auth/    : user:42
rl : decorator   : strict_api   : burst : ip:127.0.0.1
```

Each key holds a Redis hash with two fields: `tokens` and `last_refill`.

---

## Request Lifecycle (end-to-end)

```
1. Client sends GET /api/test/

2. Django routes to middleware stack

3. RateLimitMiddleware.__call__(request):
   a. get_api_scope → "api/test/"
   b. get_rule("api/test/") → matched "default"
   c. get_rate_limit_key → "ip:127.0.0.1"
   d. Build burst + sustained RedisTokenBucket objects
   e. Call allow_request() on each → runs Lua in Redis atomically
   f. Both allowed? → attach _rate_limit_info to request
   g. Call get_response(request) → Django routes to test_api view
   h. View returns JsonResponse({"message": "ok", "rate_limit": info})
   i. Middleware adds X-RateLimit-* headers to response
   j. Return response to client

4. Client receives 200 with headers:
   X-RateLimit-Policy: default
   X-RateLimit-Burst-Limit: 10
   X-RateLimit-Burst-Remaining: 9
   X-RateLimit-Sustained-Limit: 100
   X-RateLimit-Sustained-Remaining: 99
```

If either bucket is empty at step (e), the middleware returns 429 at step (f) and the view is never called.

---

## Common Mistakes to Avoid

| Mistake | Why it breaks | Fix |
|---|---|---|
| Running Lua logic in Python with two separate Redis calls | Race condition under concurrent load | Keep everything in the Lua script |
| Using a relative path for the Lua file | Breaks when Django starts from a different directory | Use `settings.BASE_DIR / "lua" / "token_bucket.lua"` |
| Opening a new Redis connection per request | Slow; exhausts file descriptors | Use a shared `ConnectionPool` at module level |
| Crashing on Redis errors | Takes down the whole service if Redis restarts | Catch `RedisError` and fail-open |
| Putting `RateLimitMiddleware` before `AuthenticationMiddleware` | `request.user` isn't populated yet — per-user keys won't work | Always place it after `AuthenticationMiddleware` in `MIDDLEWARE` |
