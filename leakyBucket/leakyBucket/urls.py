"""
URL configuration for the leakyBucket rate-limiter project.

Adding a new API:
    1. Create a view in views.py.
    2. Add a path() entry here.
    3. (Optional) Add a matching prefix rule in settings.RATE_LIMIT_RULES
       if you want custom limits for that route.

That's it — no changes to middleware or limiter required.
"""
from django.contrib import admin
from django.urls import path

from .views import test_api, strict_api, ping

urlpatterns = [
    path("admin/",       admin.site.urls),
    path("api/test/",    test_api,   name="test_api"),
    path("api/strict/",  strict_api, name="strict_api"),
    path("api/ping/",    ping,       name="ping"),
]
