"""Range jobs keep padded pieces, not one source: source lookups, recut, the
editor's monitor and retention must resolve the piece that holds the range."""
import asyncio
import json
import os
import time

import httpx
import pytest

import source_ranges

app = pytest.importorskip("app")
import recut  # noqa: E402

JOB = "pieces-job"
PIECES = [{"file": "T.piece_1.mp4", "start": 90.0, "end": 140.0},
          {"file": "T.piece_2.mp4", "start": 390.0, "end": 450.0}]


def _request(method, path, body=None):
    async def _do():
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.request(method, path, json=body)
    return asyncio.run(_do())


@pytest.fixture()
def job(tmp_path, monkeypatch):
    out, up = tmp_path / "output", tmp_path / "uploads"
    d = out / JOB
    d.mkdir(parents=True)
    up.mkdir()
    monkeypatch.setattr(app, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(up))
    for p in PIECES:
        (d / p["file"]).write_bytes(b"piece")
    (d / "T_clip_1.mp4").write_bytes(b"canonical")
    clips = [{"start": 100.0, "end": 130.0, "video_url": f"/videos/{JOB}/T_clip_1.mp4"},
             {"start": 400.0, "end": 440.0, "video_url": f"/videos/{JOB}/T_clip_2.mp4"}]
    (d / "T_clip_2.mp4").write_bytes(b"canonical")
    meta = {"shorts": clips, "source_video": "T.piece_1.mp4", "source_pieces": PIECES,
            "source_duration": 600.0, "output_format": "auto",
            "transcript": {"language": "en", "segments": [{
                "start": 0, "end": 600, "text": "a b",
                "words": [{"word": "a", "start": 405.0, "end": 405.5}]}]}}
    (d / "T_metadata.json").write_text(json.dumps(meta))
    app.jobs[JOB] = {"status": "completed", "logs": [], "user_id": None, "watermark": False,
                     "result": {"clips": [dict(c) for c in clips], "cost_analysis": {}}}
    yield d
    app.jobs.pop(JOB, None)


def test_pick_piece_needs_one_piece_holding_the_whole_range():
    assert source_ranges.pick_piece(PIECES, 100, 130) == PIECES[0]
    assert source_ranges.pick_piece(PIECES, 400, 440) == PIECES[1]
    assert source_ranges.pick_piece(PIECES, 130, 400) is None   # spans the gap
    assert source_ranges.pick_piece(PIECES, 10, 20) is None
    assert source_ranges.pick_piece(PIECES) == PIECES[0]


def test_locate_range_returns_piece_and_offset(job):
    path, off = app._locate_source_for_range(JOB, 400, 440)
    assert os.path.basename(path) == "T.piece_2.mp4" and off == 390.0
    assert app._locate_source_for_range(JOB, 10, 20) == (None, 0.0)
    assert os.path.basename(app._locate_source(JOB)) == "T.piece_1.mp4"


def test_locate_legacy_job_without_pieces_still_works(job):
    meta = json.loads((job / "T_metadata.json").read_text())
    meta.pop("source_pieces")
    (job / "T_metadata.json").write_text(json.dumps(meta))
    assert app._locate_source_for_range(JOB, 400, 440) == (str(job / "T.piece_1.mp4"), 0.0)


def test_edl_serves_the_clips_piece_with_its_offset(job):
    data = _request("GET", f"/api/clip/{JOB}/1/edl").json()
    assert data["source"]["offset"] == 390.0
    assert data["source"]["duration"] == 600.0
    assert "start=400.000" in data["source"]["url"]
    served = _request("GET", data["source"]["url"])
    assert served.status_code == 200 and served.content == b"piece"


@pytest.fixture()
def fake_recut(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        name = f"recut_1_{kw['clean_name']}"
        open(os.path.join(kw["output_dir"], name), "wb").write(b"r")
        return name, name
    monkeypatch.setattr(recut, "perform_recut", fake)
    return calls


def test_rerender_source_path_rebases_to_piece(job, fake_recut):
    r = _request("POST", "/api/clip/rerender", {
        "job_id": JOB, "clip_index": 1, "framing": "track",
        "segments": [{"start": 405.0, "end": 425.0}]})
    assert r.status_code == 200, r.text
    (call,) = fake_recut
    assert os.path.basename(call["input_path"]) == "T.piece_2.mp4"
    assert call["segments"] == [{"start": 15.0, "end": 35.0}]


def test_segments_outside_every_piece_are_a_409_not_a_500(job, fake_recut):
    r = _request("POST", "/api/clip/rerender", {
        "job_id": JOB, "clip_index": 1, "framing": "track",
        "segments": [{"start": 200.0, "end": 220.0}]})
    assert r.status_code == 409 and "not kept" in r.json()["detail"]
    assert not fake_recut


def test_retention_removes_every_piece(job, monkeypatch):
    monkeypatch.setattr(app, "SOURCE_RETENTION_SECONDS", 10)
    old = time.time() - 100
    for p in PIECES:
        os.utime(job / p["file"], (old, old))
    assert list(app._sweep_retained_sources(time.time())) == [JOB]
    assert not [f for f in os.listdir(job) if ".piece_" in f]


def test_non_owner_is_refused_on_the_source_endpoint_with_a_range(job, monkeypatch):
    monkeypatch.setattr(app, "BILLING_ENABLED", True)
    app.jobs[JOB]["user_id"] = "someone-else"

    async def nobody(request):
        return None
    monkeypatch.setattr(app, "_user_from_request", nobody)
    r = _request("GET", f"/api/source/{JOB}?start=100&end=130")
    assert r.status_code == 404 and r.content != b"piece"
