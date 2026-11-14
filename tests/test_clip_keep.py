"""Per-clip protection from the retention sweep (.keep.json): the keep API,
pruning an expired job to its kept clips, /api/status after a prune, and the
size cap. Fake time only: the sweeps take ``now``; no sleeps."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

app_module = pytest.importorskip("app")

JOB = "keepjob"
OLD = 3 * 24 * 3600
KEPT_FILE = "subtitled_200_hooked_100_base_clip_2.mp4"


def _age(path, seconds, now=None):
    t = (time.time() if now is None else now) - seconds
    os.utime(path, (t, t))


@pytest.fixture
def disk(tmp_path, monkeypatch):
    out, up = tmp_path / "output", tmp_path / "uploads"
    out.mkdir()
    up.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(out / "thumbnails"))
    monkeypatch.setattr(app_module, "JOB_RETENTION_SECONDS", 86400)
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    monkeypatch.setattr(app_module, "_running_jobs", set())
    monkeypatch.setattr(app_module, "jobs", {})
    return out


def _job(out, age=OLD):
    """A finished 3-clip job as main.py leaves it: metadata, clean reframes,
    derived (captioned/hooked) files, a source piece, audio, a subdir."""
    d = out / JOB
    d.mkdir()
    (d / "base_metadata.json").write_text(json.dumps(
        {"shorts": [{"title": f"c{i}"} for i in range(3)]}))
    for name in ("base_clip_1.mp4", "subtitled_100_base_clip_1.mp4",
                 "base_clip_2.mp4", "hooked_100_base_clip_2.mp4", KEPT_FILE,
                 "base_clip_3.mp4", "source_piece_0.mp4", "audio.wav", "subs_1.ass"):
        (d / name).write_bytes(b"x" * 100)
        _age(d / name, 1000)
    _age(d / KEPT_FILE, 10)  # newest derived file of clip 2: what status serves
    (d / ".owner").write_text("")
    (d / "frames").mkdir()
    (d / "frames" / "f.jpg").write_bytes(b"x")
    _age(d, age)
    return d


def _keep(d, idx, until):
    app_module._write_keeps(str(d), {idx: until})


def _req(method, path, **kw):
    async def go():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(go())


def _later(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


# --- API -----------------------------------------------------------------

class TestKeepApi:
    def test_set_replace_delete(self, disk):
        d = _job(disk)
        mtime = os.path.getmtime(d)
        url = f"/api/jobs/{JOB}/clips/1/keep"
        r = _req("POST", url, json={"until": (_later(days=3)).isoformat()})
        assert r.status_code == 200
        assert set(json.loads((d / ".keep.json").read_text())) == {"1"}
        until2 = _later(days=5).replace(microsecond=0)
        r = _req("POST", url, json={"until": until2.isoformat().replace("+00:00", "Z")})
        assert r.status_code == 200
        assert app_module._read_keeps(str(d)) == {1: until2}
        assert _req("DELETE", url).status_code == 200
        assert json.loads((d / ".keep.json").read_text()) == {}
        assert os.path.getmtime(d) == mtime  # the retention clock is untouched

    def test_until_is_clamped_to_61_days(self, disk):
        d = _job(disk)
        r = _req("POST", f"/api/jobs/{JOB}/clips/0/keep", json={"until": _later(days=400).isoformat()})
        assert r.status_code == 200
        until = app_module._read_keeps(str(d))[0]
        assert timedelta(days=60, hours=23) < until - datetime.now(timezone.utc) <= timedelta(days=61)

    @pytest.mark.parametrize("index,until", [
        (3, None), (-1, None), (0, "yesterday"), (0, "2020-01-01T00:00:00Z")])
    def test_bad_index_or_time_is_400(self, disk, index, until):
        d = _job(disk)
        body = {"until": until or _later(days=1).isoformat()}
        assert _req("POST", f"/api/jobs/{JOB}/clips/{index}/keep", json=body).status_code == 400
        assert not (d / ".keep.json").exists()

    def test_unknown_job_is_404(self, disk):
        r = _req("POST", "/api/jobs/nope/clips/0/keep", json={"until": _later(days=1).isoformat()})
        assert r.status_code == 404
        assert _req("DELETE", "/api/jobs/nope/clips/0/keep").status_code == 404

    def test_cloud_is_404(self, disk, monkeypatch):
        d = _job(disk)
        monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
        url = f"/api/jobs/{JOB}/clips/0/keep"
        assert _req("POST", url, json={"until": _later(days=1).isoformat()}).status_code == 404
        assert _req("DELETE", url).status_code == 404
        assert not (d / ".keep.json").exists()

    def test_owner_check_refuses(self, disk, monkeypatch):
        d = _job(disk)

        async def refuse(request, record):
            raise app_module.HTTPException(status_code=404, detail="Not found")
        monkeypatch.setattr(app_module, "_assert_job_owner", refuse)
        url = f"/api/jobs/{JOB}/clips/0/keep"
        assert _req("POST", url, json={"until": _later(days=1).isoformat()}).status_code == 404
        assert _req("DELETE", url).status_code == 404
        assert not (d / ".keep.json").exists()

    def test_sidecar_is_never_served(self):
        assert not app_module._media_guard(f"{JOB}/.keep.json")


# --- cleanup -------------------------------------------------------------

class TestPrune:
    @pytest.mark.parametrize("in_memory", [False, True])
    def test_expired_job_keeps_exactly_the_kept_clip(self, disk, in_memory):
        d = _job(disk)
        if in_memory:  # served names as run_job recorded them (CLIP_READY)
            app_module.jobs[JOB] = {"status": "completed", "logs": [], "result": {"clips": [
                {"video_url": f"/videos/{JOB}/subtitled_100_base_clip_1.mp4"},
                {"video_url": f"/videos/{JOB}/{KEPT_FILE}"},
                {"video_url": f"/videos/{JOB}/base_clip_3.mp4"}]}}
        now = time.time()
        _keep(d, 1, datetime.fromtimestamp(now + 86400, timezone.utc))
        app_module._sweep_expired_job_dirs(now)
        left = sorted(os.listdir(d))
        assert left == sorted([".keep.json", ".owner", "base_metadata.json", KEPT_FILE])
        app_module._sweep_expired_job_dirs(now + 60)  # idempotent
        assert sorted(os.listdir(d)) == left
        assert os.path.getmtime(d) < now - 86400  # pruning did not reset the clock

    def test_status_answers_with_the_kept_clip_only(self, disk):
        d = _job(disk)
        now = time.time()
        _keep(d, 1, datetime.fromtimestamp(now + 86400, timezone.utc))
        app_module._sweep_expired_job_dirs(now)
        for restart in (False, True):
            if restart:
                app_module.jobs.clear()
            r = _req("GET", f"/api/status/{JOB}")
            assert r.status_code == 200
            clips = r.json()["result"]["clips"]
            assert [c.get("gone", False) for c in clips] == [True, False, True]
            assert clips[1]["video_url"] == f"/videos/{JOB}/{KEPT_FILE}"
            assert "video_url" not in clips[0] and "video_url" not in clips[2]
            assert (d / os.path.basename(clips[1]["video_url"])).is_file()

    @pytest.mark.parametrize("how", ["expired", "deleted"])
    def test_dir_goes_once_no_keep_is_active(self, disk, how):
        d = _job(disk)
        now = time.time()
        _keep(d, 1, datetime.fromtimestamp(now + 3600, timezone.utc))
        app_module._sweep_expired_job_dirs(now)
        assert d.exists()
        if how == "deleted":
            assert _req("DELETE", f"/api/jobs/{JOB}/clips/1/keep").status_code == 200
            app_module._sweep_expired_job_dirs(now + 60)
        else:
            app_module._sweep_expired_job_dirs(now + 3601)
        assert not d.exists() and JOB not in app_module.jobs

    def test_unresolvable_kept_clip_prunes_nothing(self, disk):
        d = _job(disk)
        (d / KEPT_FILE).unlink()
        (d / "hooked_100_base_clip_2.mp4").unlink()
        (d / "base_clip_2.mp4").unlink()
        _age(d, OLD)
        before = sorted(os.listdir(d))
        now = time.time()
        _keep(d, 1, datetime.fromtimestamp(now + 86400, timezone.utc))
        app_module._sweep_expired_job_dirs(now)
        assert sorted(os.listdir(d)) == sorted(before + [".keep.json"])

    def test_live_job_is_untouched(self, disk):
        d = _job(disk)
        app_module.jobs[JOB] = {"status": "processing", "logs": [], "result": None}
        now = time.time()
        _keep(d, 1, datetime.fromtimestamp(now + 86400, timezone.utc))
        before = sorted(os.listdir(d))
        app_module._sweep_expired_job_dirs(now)
        assert sorted(os.listdir(d)) == before

    def test_no_keep_file_is_the_old_behaviour(self, disk):
        d = _job(disk)
        result = {"clips": [{"video_url": f"/videos/{JOB}/gone.mp4"}]}
        assert app_module._mark_pruned_clips(JOB, result) is result
        app_module._sweep_expired_job_dirs(time.time())
        assert not d.exists()

    def test_size_cap_never_deletes_kept_files(self, disk, monkeypatch):
        d = _job(disk, age=60)  # not expired: only the cap is at work
        other = disk / "other"
        other.mkdir()
        (other / "x.mp4").write_bytes(b"x" * 100)
        _age(other, 30)
        _keep(d, 1, _later(days=1))
        monkeypatch.setattr(app_module, "OUTPUT_MAX_GB", 1 / 1024 ** 3)  # 1 byte
        app_module._enforce_output_size_cap()
        assert sorted(os.listdir(d)) == sorted([".keep.json", ".owner", "base_metadata.json", KEPT_FILE])
        assert not other.exists()
