import os
import sys

import pytest

# Make the repo root importable so tests can import the app modules directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests always run the app in self-host (BYOK) mode. app.py freezes
# BILLING_ENABLED at import time and load_dotenv never overrides an existing
# variable, so this must be set here — before any test module imports app — or
# the suite's behavior would depend on the developer's personal .env.
os.environ["BILLING_ENABLED"] = "0"

# Variables a developer .env may define (main/app/gemini_worker call
# load_dotenv() at import) that change behavior under test.
_SCRUBBED = (
    "GEMINI_API_KEY", "UPLOAD_POST_API_KEY", "PUBLIC_API_URL", "YOUTUBE_COOKIES",
    "PROXY_URL", "STATIC_PROXY_URLS",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    for name in list(os.environ):
        if name in _SCRUBBED or name.startswith(("LLM_", "TELEGRAM_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def main():
    """``main`` importable without the heavy ML stack (see main_harness)."""
    from main_harness import light_main
    with light_main() as m:
        yield m
