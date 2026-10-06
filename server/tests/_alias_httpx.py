"""Loaded as an early pytest plugin (see pyproject addopts), before respx and the app import httpx.

The app runs on one HTTP stack, httpx2 (see app/__init__.py). Aliasing here too means respx,
which patches `httpx`, intercepts every request the app makes.
"""

import httpx2

httpx2.alias_httpx()
