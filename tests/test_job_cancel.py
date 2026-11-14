"""POST /api/jobs/{id}/cancel and GET /api/jobs/active (a user cancelling a
job; tests/test_cancellation.py is about Cloud subscriptions)."""
import asyncio
import io
import json
import os
import shutil
import subprocess
import time
import types

import httpx
import pytest

app_module = pytest.importorskip("app")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(root))
    monkeypatch.setattr(app_module, "_draining", False)
    monkeypatch.setattr(app_module, "_stopping", False)
    monkeypatch.setattr(app_module, "_running_jobs", set())
    monkeypatch.setattr(app_module, "_admitting", set())
    monkeypatch.setattr(app_module, "_job_processes", {})
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "job_queue", asyncio.PriorityQueue())
    calls = {"released": [], "committed": [], "webhooks": [], "enqueued": []}

    async def release(rid):
        calls["released"].append(rid)

    async def commit(rid):
        calls["committed"].append(rid)
    # The real _settle_reservation, on a fake Cloud metering.
    monkeypatch.setattr(app_module, "cloud", types.SimpleNamespace(
        metering=types.SimpleNamespace(release_reservation=release, commit_reservation=commit)))

    async def webhook(job_id):
        calls["webhooks"].append(job_id)
    monkeypatch.setattr(app_module, "_notify_job_webhook", webhook)
    for name in ("_archive_managed_job", "_record_job_alert", "_track_proxy_usage",
                 "_notify_clips_ready", "_notify_clip_activity"):
        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(app_module, name, _noop)

    async def _noop2(*a, **k):
        return None
    monkeypatch.setattr(app_module, "_autopilot_job_finished", _noop2)
    monkeypatch.setattr(app_module, "_enqueue_job",
                        lambda job_id, priority=2: calls["enqueued"].append(job_id))
    return root, calls


def _job(root, job_id, status="queued", user_id=None, url="https://youtu.be/abc"):
    d = root / job_id
    d.mkdir()
    (d / app_module._RESUME_FILE).write_text(json.dumps({"cmd": ["x"], "priority": 2}))
    job = {"status": status, "logs": app_module._TimedLog([f"Job {job_id} queued."]),
           "cmd": ["python", "-u", "main.py", "-u", url, "-o", str(d)], "env": {},
           "output_dir": str(d), "user_id": user_id, "reservation_id": f"res-{job_id}"}
    app_module.jobs[job_id] = job
    return d, job


async def _req(method, path):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path)


def _call(method, path):
    return asyncio.run(_req(method, path))


def _billing(monkeypatch, caller):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)

    async def user(request):
        return types.SimpleNamespace(id=caller["id"]) if caller["id"] else None
    monkeypatch.setattr(app_module, "_user_from_request", user)


# --- queued -----------------------------------------------------------------
def test_queued_job_is_cancelled_released_and_never_started(env, monkeypatch):
    root, calls = env
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    monkeypatch.setattr(app_module, "_assert_job_owner", _no_owner_check)
    d, job = _job(root, "q1")
    started = []

    async def spy_wrapper(job_id):
        started.append(job_id)

    monkeypatch.setattr(app_module, "run_job_wrapper", spy_wrapper)

    async def go():
        app_module.job_queue.put_nowait((2, 0, "q1"))
        r = await _req("POST", "/api/jobs/q1/cancel")
        assert r.status_code == 200 and r.json() == {"job_id": "q1", "status": "cancelled"}
        worker = asyncio.create_task(app_module.process_queue())
        await asyncio.sleep(0.05)
        worker.cancel()
        return r
    asyncio.run(go())
    assert started == []
    assert app_module.job_queue.qsize() == 0
    assert job["status"] == "cancelled" and app_module.CANCEL_MESSAGE in list(job["logs"])
    assert not (d / app_module._RESUME_FILE).exists()
    assert (d / app_module._CANCELLED_FILE).exists()
    assert calls["released"] == ["res-q1"] and calls["committed"] == []
    assert calls["webhooks"] == ["q1"]


async def _no_owner_check(request, record):
    return None


def test_cancel_while_waiting_for_a_slot_never_starts(env, monkeypatch):
    """Dequeued and waiting on the semaphore when the cancel lands."""
    root, calls = env
    _job(root, "q2")
    started = []

    async def spy_wrapper(job_id):
        started.append(job_id)
    monkeypatch.setattr(app_module, "run_job_wrapper", spy_wrapper)

    async def go():
        sem = asyncio.Semaphore(1)
        await sem.acquire()   # every slot busy
        monkeypatch.setattr(app_module, "concurrency_semaphore", sem)
        app_module.job_queue.put_nowait((2, 0, "q2"))
        worker = asyncio.create_task(app_module.process_queue())
        await asyncio.sleep(0.02)
        assert "q2" in app_module._admitting
        assert (await _req("POST", "/api/jobs/q2/cancel")).status_code == 200
        sem.release()
        await asyncio.sleep(0.05)
        worker.cancel()
        assert sem.locked() is False   # the slot was handed back
    asyncio.run(go())
    assert started == []


# --- running ----------------------------------------------------------------
class FakePopen:
    def __init__(self, *a, **k):
        self.pid = 2 ** 22 + 7   # never killed for real: _kill_job_process is stubbed
        self.returncode = None
        self.stdout = io.BytesIO(b"")
        FakePopen.last = self

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_running_job_process_is_killed_and_job_settled(env, monkeypatch):
    root, calls = env
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    monkeypatch.setattr(app_module, "_assert_job_owner", _no_owner_check)
    monkeypatch.setattr(app_module.subprocess, "Popen", FakePopen)
    killed = []

    def fake_kill(process):
        killed.append(process)
        process.kill()
    monkeypatch.setattr(app_module, "_kill_job_process", fake_kill)
    d, job = _job(root, "r1")

    async def go():
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        monkeypatch.setattr(app_module, "concurrency_semaphore", sem)
        app_module.job_queue.put_nowait((2, 0, "r1"))
        await app_module.job_queue.get()
        wrapper = asyncio.create_task(app_module.run_job_wrapper("r1"))
        for _ in range(100):
            if "r1" in app_module._job_processes:
                break
            await asyncio.sleep(0.01)
        assert job["status"] == "processing" and job.get("started_at")
        r = await _req("POST", "/api/jobs/r1/cancel")
        assert r.status_code == 200
        await asyncio.wait_for(wrapper, 10)
        assert sem.locked() is False   # the slot is free again
    asyncio.run(go())
    assert killed == [FakePopen.last]
    assert job["status"] == "cancelled"   # not "failed" from exit code -9
    assert calls["enqueued"] == []        # so no auto-retry
    assert not (d / app_module._RESUME_FILE).exists()
    assert calls["released"] == ["res-r1"] and calls["committed"] == []
    assert "r1" not in app_module._job_processes


def test_cancel_lands_before_the_process_exists(env, monkeypatch):
    root, calls = env
    _d, job = _job(root, "r2")
    job["cancel_requested"] = True
    monkeypatch.setattr(app_module.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    asyncio.run(app_module.run_job("r2", job))
    assert job["status"] == "queued" and "started_at" not in job


@pytest.mark.skipif(os.name == "nt" or not shutil.which("ps"), reason="POSIX ps")
def test_kill_reaches_the_grandchildren():
    """main.py's ffmpeg children die with it (they share the API's group)."""
    p = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE)
    grandchild = int(p.stdout.readline())
    assert grandchild in app_module._descendant_pids(p.pid)
    app_module._kill_job_process(p)
    p.wait(5)
    assert _gone(grandchild)


def _gone(pid, seconds=3.0):
    """Dead: no such process, or a zombie its new parent has not reaped yet."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


# --- refusals -----------------------------------------------------------------
def test_unknown_404_and_finished_409(env):
    root, _ = env
    assert _call("POST", "/api/jobs/nope/cancel").status_code == 404
    for st in ("completed", "failed", "cancelled"):
        _d, job = _job(root, f"done-{st}", status=st)
        r = _call("POST", f"/api/jobs/done-{st}/cancel")
        assert r.status_code == 409 and st in r.json()["detail"]
        assert job["status"] == st


def test_non_owner_is_refused(env, monkeypatch):
    root, calls = env
    caller = {"id": "user-b"}
    _billing(monkeypatch, caller)
    d, job = _job(root, "own1", user_id="user-a")
    assert _call("POST", "/api/jobs/own1/cancel").status_code == 404
    assert job["status"] == "queued" and (d / app_module._RESUME_FILE).exists()
    assert calls["released"] == []
    caller["id"] = "user-a"
    assert _call("POST", "/api/jobs/own1/cancel").status_code == 200


# --- the drain path is unchanged -----------------------------------------------
def _wrap(monkeypatch, job_id, final_status, raise_cancel=False, cancel=False):
    async def fake_run_job(jid, job):
        if cancel:
            job["cancel_requested"] = True
        if raise_cancel:
            raise asyncio.CancelledError()
        job["status"] = final_status
    monkeypatch.setattr(app_module, "run_job", fake_run_job)

    async def go():
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        monkeypatch.setattr(app_module, "concurrency_semaphore", sem)
        app_module.job_queue.put_nowait((1, 0, job_id))
        await app_module.job_queue.get()
        try:
            await app_module.run_job_wrapper(job_id)
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_drain_cancel_still_keeps_the_manifest(env, monkeypatch):
    root, calls = env
    d, job = _job(root, "dr1", status="processing")
    _wrap(monkeypatch, "dr1", "processing", raise_cancel=True)
    assert (d / app_module._RESUME_FILE).exists() and calls["released"] == []
    monkeypatch.setattr(app_module, "_stopping", True)
    d2, _ = _job(root, "dr2", status="processing")
    _wrap(monkeypatch, "dr2", "failed")
    assert (d2 / app_module._RESUME_FILE).exists()


def test_user_cancel_during_shutdown_is_still_final(env, monkeypatch):
    root, calls = env
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    monkeypatch.setattr(app_module, "_stopping", True)
    d, _ = _job(root, "dr3", status="processing")
    _wrap(monkeypatch, "dr3", "cancelled", cancel=True)
    assert not (d / app_module._RESUME_FILE).exists()
    assert calls["released"] == ["res-dr3"]


def test_recovered_cancelled_job_stays_cancelled(env):
    root, _ = env
    d = root / "rec1"
    d.mkdir()
    (d / "v_metadata.json").write_text(json.dumps({"shorts": []}))
    (d / app_module._CANCELLED_FILE).write_text("")
    app_module._recover_jobs_from_disk()
    assert app_module.jobs["rec1"]["status"] == "cancelled"


# --- active list ----------------------------------------------------------------
def test_active_lists_queued_and_processing_with_position(env, monkeypatch):
    root, _ = env
    _job(root, "a1", status="processing")
    app_module.jobs["a1"]["started_at"] = 123.0
    _job(root, "a2", url="https://youtu.be/two")
    _job(root, "a3", status="completed")
    app_module.job_queue.put_nowait((2, 0, "a2"))
    out = _call("GET", "/api/jobs/active").json()["jobs"]
    by_id = {j["job_id"]: j for j in out}
    assert set(by_id) == {"a1", "a2"}
    assert by_id["a1"]["status"] == "processing" and by_id["a1"]["started_at"] == 123.0
    assert by_id["a2"]["position"] == 1 and by_id["a2"]["source"] == "https://youtu.be/two"
    assert by_id["a1"]["stage"] == "Job a1 queued."


def test_active_is_scoped_to_the_caller_in_cloud(env, monkeypatch):
    root, _ = env
    caller = {"id": "user-a"}
    _billing(monkeypatch, caller)

    async def owner(request):
        return caller["id"]
    monkeypatch.setattr(app_module, "_owner_id", owner)
    _job(root, "mine", user_id="user-a")
    _job(root, "theirs", user_id="user-b")
    assert [j["job_id"] for j in _call("GET", "/api/jobs/active").json()["jobs"]] == ["mine"]
    caller["id"] = None
    assert _call("GET", "/api/jobs/active").status_code == 401
