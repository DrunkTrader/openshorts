"""app.lifespan starts the intake bot only for self-host with a token."""
import asyncio
import sys

import pytest

app_module = pytest.importorskip("app")


def _run_lifespan(monkeypatch, tmp_path, billing, token):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "BILLING_ENABLED", billing)
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "job_queue", asyncio.PriorityQueue())
    if token:
        monkeypatch.setenv("TELEGRAM_INTAKE_TOKEN", "t")
    calls = []

    async def go():
        async with app_module.lifespan(app_module.app):
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    if token and not billing:
        import telegram_bot

        async def rec(is_active=None, env=None):
            calls.append(is_active)
        monkeypatch.setattr(telegram_bot, "run_from_env", rec)
    asyncio.run(go())
    return calls


def test_starts_task_when_token_set_and_billing_off(monkeypatch, tmp_path):
    calls = _run_lifespan(monkeypatch, tmp_path, False, True)
    assert len(calls) == 1 and calls[0]() is True


def test_nothing_imported_without_token(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "telegram_bot", raising=False)
    _run_lifespan(monkeypatch, tmp_path, False, False)
    assert "telegram_bot" not in sys.modules


def test_billing_on_starts_nothing(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS
    monkeypatch.delitem(sys.modules, "telegram_bot", raising=False)

    async def noop(*a, **k):
        return None
    stub = NS(setup_async=noop, account=NS(register_local_purge=lambda f: None),
              videos=NS(register_local_unmark=lambda f: None),
              autopilot=NS(start=lambda *a, **k: None),
              lifecycle=NS(start=lambda *a, **k: None))
    monkeypatch.setattr(app_module, "cloud", stub)
    monkeypatch.setattr(app_module, "_alerts", NS(proxy_watch_loop=noop), raising=False)
    _run_lifespan(monkeypatch, tmp_path, True, True)
    assert "telegram_bot" not in sys.modules
