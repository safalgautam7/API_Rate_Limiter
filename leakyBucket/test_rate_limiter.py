# -*- coding: utf-8 -*-
"""
Integration tests for the leakyBucket rate limiter.
Run with:  python test_rate_limiter.py
Requires the Django dev server running on http://127.0.0.1:8000
"""

import urllib.request
import urllib.error
import json
import sys
import redis as redis_lib

BASE = "http://127.0.0.1:8000"
failures = 0


def get(path):
    try:
        r = urllib.request.urlopen(f"{BASE}{path}")
        body = json.loads(r.read())
        headers = {k.lower(): v for k, v in r.headers.items()}
        return r.status, body, headers
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        headers = {k.lower(): v for k, v in e.headers.items()}
        return e.code, body, headers


def check(label, condition, detail=""):
    global failures
    if condition:
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" << {detail}" if detail else ""))
        failures += 1


rc = redis_lib.Redis.from_url("redis://127.0.0.1:6379/1")
rc.delete(*[k for k in rc.scan_iter("rl:*")])


print("=== Test 1 ===")
status, body, hdrs = get("/api/test/")
check("Status 200", status == 200)
check("Body ok", body.get("message") == "ok")
check("rate_limit exists", "rate_limit" in body)


print("\n=== Test 2 ===")
check("policy header", "x-ratelimit-policy" in hdrs)
check("burst header", "x-ratelimit-burst-limit" in hdrs)
check("sustained header", "x-ratelimit-sustained-limit" in hdrs)


print("\n=== Test 3 ===")
rc.delete(*[k for k in rc.scan_iter("rl:*")])
codes = []
for _ in range(15):
    s, _, _ = get("/api/test/")
    codes.append(s)

check("200 exists", 200 in codes)
check("429 exists", 429 in codes)


print("\n=== Test 4 ===")
rc.delete(*[k for k in rc.scan_iter("rl:*")])
for _ in range(12):
    get("/api/test/")

status, body, hdrs = get("/api/test/")
if status == 429:
    check("error field", "error" in body)
    check("retry_after", "retry_after" in body)


print("\n=== Test 5 ===")
rc.delete(*[k for k in rc.scan_iter("rl:*")])
status, body, hdrs = get("/api/ping/")
check("pong endpoint", body.get("status") == "pong")


print("\n=== Test 6 ===")
rc.delete(*[k for k in rc.scan_iter("rl:*")])
codes = []
for _ in range(7):
    s, _, _ = get("/api/strict/")
    codes.append(s)

check("allowed requests", 200 in codes)
check("blocked requests", 429 in codes)


print("\n=== Test 7 ===")
try:
    r2 = urllib.request.urlopen(f"{BASE}/admin/")
    admin_hdrs = {k.lower(): v for k, v in r2.headers.items()}
    admin_status = r2.status
except urllib.error.HTTPError as e:
    admin_hdrs = {k.lower(): v for k, v in e.headers.items()}
    admin_status = e.code

check("admin reachable", admin_status != 500)
check("no rate limit on admin", "x-ratelimit-policy" not in admin_hdrs)


print("\n====================")
if failures == 0:
    print("All tests passed!")
    sys.exit(0)
else:
    print(f"{failures} tests failed")
    sys.exit(1)