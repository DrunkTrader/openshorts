"""/api/clip/split: guards, lock, recipe, layout sidecar, wm_ copy. The render
is stubbed at slot_split.perform_split; the upload library is real files."""
import asyncio
import json
import os

import httpx
import pytest

app_module = pytest.importorskip("app")
import layout_ranges  # noqa: E402
import slot_library  # noqa: E402
import slot_split  # noqa: E402

JOB_ID = "slot-split-endpoint-job"
TRANSCRIPT = {"language": "en", "segments": [
    {"start": 0.0, "end": 60.0, "text": "hello world",
     "words": [{"word": "hello", "start": 12.0, "end": 12.5}]}]}


def _post(body):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/api/clip/split", json=body)
    return asyncio.run(_do())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    out, up, th = tmp_path / "output", tmp_path / "uploads", tmp_path / "thumbs"
    job_dir = out / JOB_ID
    job_dir.mkdir(parents=True)
    for d in (up, th):
        d.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(th))
    clip = {"start": 10.0, "end": 40.0, "video_url": f"/videos/{JOB_ID}/m_clip_1.mp4"}
    (job_dir / "m_metadata.json").write_text(json.dumps({
        "shorts": [clip], "transcript": TRANSCRIPT, "source_video": "src.mp4",
        "output_format": "auto"}))
    (job_dir / "m_clip_1.mp4").write_bytes(b"canonical")
    (job_dir / "src.mp4").write_bytes(b"source")
    app_module.jobs[JOB_ID] = {"status": "completed", "logs": [], "user_id": None,
                               "result": {"clips": [dict(clip)]}, "watermark": False}
    vdir = up / "slot_library" / "local"
    vdir.mkdir(parents=True)
    src = tmp_path / "up.mp4"
    src.write_bytes(b"u")
    item = slot_library.add(str(vdir), str(th / "slot_local"), str(src), ".mp4", "up.mp4",
                            {"width": 320, "height": 240, "duration": 2.0, "has_audio": True})
    yield {"job_dir": job_dir, "item": item, "meta": job_dir / "m_metadata.json"}
    app_module.jobs.pop(JOB_ID, None)
    app_module._rerender_locks.pop(JOB_ID, None)


@pytest.fixture()
def fake_split(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        name = f"recut_1_aaaaaa_{kw['clean_name']}"
        path = os.path.join(kw["output_dir"], name)
        open(path, "wb").write(b"r")
        slot_split.write_layout(path, 30.0, 1920, kw["spec"]["ratio"])
        return name, name
    monkeypatch.setattr(slot_split, "perform_split", fake)
    return calls


def _body(env, **over):
    body = {"job_id": JOB_ID, "clip_index": 0, "slots": {
        "top": {"kind": "source"},
        "bottom": {"kind": "upload", "upload_id": env["item"]["id"]},
        "ratio": "60/40"}}
    body.update(over)
    return body


def test_renders_records_recipe_and_layout_ranges(env, fake_split):
    r = _post(_body(env))
    assert r.status_code == 200, r.text
    call = fake_split[0]
    assert call["source_path"].endswith("src.mp4")
    assert call["captions_transcript"]["segments"][0]["words"]
    assert call["uploads"][env["item"]["id"]]["width"] == 320
    clip = json.loads(env["meta"].read_text())["shorts"][0]
    assert clip["recipe"]["split_slots"]["ratio"] == "60/40"
    assert clip["recipe"]["split_slots"]["bottom"]["upload_id"] == env["item"]["id"]
    assert clip["video_url"] == r.json()["new_video_url"]
    assert clip["layout_ranges"] == [{"start": 0.0, "end": 30.0, "layout": "split", "seam": 0.6}]
    # the canonical file is untouched
    assert (env["job_dir"] / "m_clip_1.mp4").read_bytes() == b"canonical"


def test_uses_the_per_job_rerender_lock(env, fake_split):
    seen = []
    real = app_module._split_locked

    async def spy(*a, **k):
        seen.append(app_module._rerender_locks[JOB_ID].locked())
        return await real(*a, **k)
    app_module._split_locked = spy
    try:
        assert _post(_body(env)).status_code == 200
    finally:
        app_module._split_locked = real
    assert seen == [True]


@pytest.mark.parametrize("slots", [
    {"top": {"kind": "bogus"}},
    {"ratio": "50/50"},
    {"top": {"kind": "upload", "upload_id": "f" * 32}},       # not in the library
    {"top": {"kind": "source"}, "ratio": "99/1"},
])
def test_bad_requests_never_render(env, fake_split, slots):
    r = _post(_body(env, slots=slots))
    assert r.status_code in (400, 404)
    assert fake_split == []


def test_upload_audio_requires_an_audio_track(env, fake_split):
    vdir = os.path.join(app_module.UPLOAD_DIR, "slot_library", "local")
    idx = json.load(open(os.path.join(vdir, "index.json")))
    idx[env["item"]["id"]]["has_audio"] = False
    json.dump(idx, open(os.path.join(vdir, "index.json"), "w"))
    body = _body(env)
    body["slots"]["audio"] = "mix"
    assert _post(body).status_code == 400
    assert fake_split == []


def test_gone_source_and_wrong_format(env, fake_split):
    os.remove(env["job_dir"] / "src.mp4")
    assert _post(_body(env)).status_code == 409
    meta = json.loads(env["meta"].read_text())
    meta["output_format"] = "square"
    env["meta"].write_text(json.dumps(meta))
    assert _post(_body(env)).status_code == 400


def test_free_plan_serves_wm_copy_only(env, fake_split, monkeypatch):
    """The served name is what _deliver returns; the canonical stays clean.
    Cloud flag is off in tests, so _deliver itself is stubbed to mark."""
    async def deliver(request, job_id, filename):
        return f"wm_{filename}"
    monkeypatch.setattr(app_module, "_deliver", deliver)
    r = _post(_body(env))
    assert r.json()["new_video_url"].rsplit("/", 1)[1].startswith("wm_")
    assert (env["job_dir"] / "m_clip_1.mp4").read_bytes() == b"canonical"
    assert layout_ranges.read(str(env["job_dir"] / "recut_1_aaaaaa_m_clip_1.mp4"))
