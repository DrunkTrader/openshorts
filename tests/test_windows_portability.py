"""Windows/native portability: no POSIX-only call may crash a job or the API."""
import asyncio
import subprocess

import pytest

import gpu_decode
import gpu_render


def _no_nvidia_smi(*a, **kw):
    raise FileNotFoundError("nvidia-smi")


def test_job_env_forces_utf8_on_windows():
    import app
    env = app._child_env({"A": "1"}, nt=True)
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert "PYTHONUTF8" not in app._child_env({"A": "1"}, nt=False)
    assert app._child_env({"PYTHONUTF8": "0"}, nt=True)["PYTHONUTF8"] == "0"


def test_job_encode_env_skips_fcntl_slots_on_windows(monkeypatch):
    import app
    monkeypatch.setattr(app.os, "name", "nt")
    assert "NVENC_SLOTS" not in app._job_encode_env({"A": "1"})


def test_drain_handler_tolerates_loops_without_signal_support(monkeypatch):
    import app

    async def run():
        loop = asyncio.get_running_loop()

        def boom(*a, **kw):
            raise NotImplementedError
        monkeypatch.setattr(loop, "add_signal_handler", boom)
        app._install_drain_signal_handler()

    asyncio.run(run())


def test_gpu_probes_return_none_without_nvidia_smi(monkeypatch):
    import app
    monkeypatch.setattr(subprocess, "run", _no_nvidia_smi)
    monkeypatch.setattr(app, "_gpu_free_cache", {"at": 0.0, "mb": None})
    assert app._gpu_free_mb() is None
    monkeypatch.setattr(gpu_decode, "_free", {"t": 0.0, "mb": None})
    assert gpu_decode._free_mb() is None


def test_nvenc_slot_is_a_noop_without_fcntl(monkeypatch, tmp_path):
    monkeypatch.setenv("NVENC_SLOTS", "2")
    monkeypatch.setenv("NVENC_LOCK_DIR", str(tmp_path))
    monkeypatch.setitem(__import__("sys").modules, "fcntl", None)  # import raises ImportError
    with gpu_render.nvenc_slot():
        pass


def test_cookies_path_falls_back_to_temp_dir_without_app(monkeypatch, tmp_path):
    import yt_clients
    monkeypatch.delenv("COOKIES_FILE", raising=False)
    monkeypatch.setattr(yt_clients.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(yt_clients.tempfile, "gettempdir", lambda: str(tmp_path))
    path = yt_clients.cookies_path()
    assert path == str(tmp_path / "openshorts_cookies.txt")
    monkeypatch.setenv("COOKIES_FILE", "/x/c.txt")
    assert yt_clients.cookies_path() == "/x/c.txt"


def test_cookies_file_is_written_private_and_never_logged(main, monkeypatch, tmp_path, capsys):
    import os
    import stat
    import yt_clients
    monkeypatch.delenv("COOKIES_FILE", raising=False)
    monkeypatch.setattr(yt_clients.os.path, "isdir", lambda p: False)  # no /app
    monkeypatch.setattr(yt_clients.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("YOUTUBE_COOKIES", "SECRET-SESSION-é")
    path = yt_clients.cookies_path()
    open(path, "w").close()
    if os.name != "nt":
        os.chmod(path, 0o644)  # pre-existing loose file must be tightened

    assert main._write_cookies_file(path) == path
    with open(path, encoding="utf-8") as f:
        assert f.read() == "SECRET-SESSION-é"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "SECRET-SESSION" not in capsys.readouterr().out
    monkeypatch.delenv("YOUTUBE_COOKIES")
    assert main._write_cookies_file(path) is None
