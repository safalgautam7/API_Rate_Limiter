# Django Rate Limiter (Redis + Lua, Atomic)

A production-style rate limiter for Django using a **Token Bucket algorithm** with **atomic execution via Redis Lua scripts**.

---

## How it works

Each user/IP gets two token buckets:

- **Burst** — short-term allowance (high refill rate)
- **Sustained** — long-term throughput cap (low refill rate)

Every request consumes one token from each. If either bucket is empty the request is blocked with `429 Too Many Requests`.

The entire check runs inside a Lua script executed atomically in Redis — no race conditions.

---

## Tech Stack

- Python + Django
- Redis
- Lua (atomic operations)

---

## Setup

```bash
# 1. Clone
git clone https://github.com/your-username/rate-limiter.git
cd rate-limiter/leakyBucket

# 2. Install dependencies
uv sync

# 3. Start Redis
docker run -d -p 6379:6379 redis

# 4. Run Django
.venv\Scripts\activate
uv run python manage.py migrate
uv run python manage.py runserver
```

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/test/` | Returns current rate-limit state in the response body |
| `GET /api/strict/` | Tighter per-view limits via `@rate_limit` decorator (burst=3) |
| `GET /api/ping/` | Minimal health-check, uses global default limits |

---

## Adding a new API

1. Write a view in `views.py`
2. Register it in `urls.py`
3. *(Optional)* Add a prefix rule in `settings.RATE_LIMIT_RULES` for custom limits

No changes to middleware or limiter needed.

---

## Configuring limits (`settings.py`)

```python
RATE_LIMIT_ENABLED = True  # set False to bypass globally

RATE_LIMIT_RULES = {
    "default": {
        "burst":     {"capacity": 10, "refill_rate": 5},
        "sustained": {"capacity": 100, "refill_rate": 1},
    },
    # "api/auth/": {
    #     "burst":     {"capacity": 5, "refill_rate": 2},
    #     "sustained": {"capacity": 30, "refill_rate": 1},
    # },
}
```

Rules are matched by **longest URL prefix**. `"default"` is the fallback.

---

## Per-view limits (decorator)

```python
from leakyBucket.decorators import rate_limit

@rate_limit(burst={"capacity": 3, "refill_rate": 1},
            sustained={"capacity": 10, "refill_rate": 1})
def my_view(request):
    ...
```

---

## Response headers

Every non-blocked response includes:

```
X-RateLimit-Policy: default
X-RateLimit-Burst-Limit: 10
X-RateLimit-Burst-Remaining: 7
X-RateLimit-Sustained-Limit: 100
X-RateLimit-Sustained-Remaining: 98
```

Blocked responses (`429`) include a `Retry-After` header.

---

## Testing

```bash
python test_rate_limiter.py
```

Or manually with PowerShell:

```powershell
for ($i=1; $i -le 20; $i++) {
    try { (Invoke-WebRequest http://127.0.0.1:8000/api/test/).StatusCode }
    catch { $_.Exception.Response.StatusCode.value__ }
}
```

Expected: first 10 requests → `200`, then `429`, recovers after a few seconds.

---

## Features

- Per-user limiting (authenticated) / per-IP fallback (anonymous)
- Burst + sustained dual-bucket limiting
- Settings-driven rules, no middleware edits needed for new APIs
- View-level `@rate_limit` decorator for one-off overrides
- Fail-open on Redis errors (outage doesn't take down the service)
- Atomic Lua script execution (no race conditions)

---

## Future Improvements

- Sliding window log algorithm
- Redis cluster support
- API key-based throttling
- Load testing benchmarks
