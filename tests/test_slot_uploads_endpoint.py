"""/api/slot-uploads: the trust boundary (content, not extension or MIME),
size cap, filename handling, and owner scoping of list/delete."""
import asyncio
import os
import shutil
import subprocess

import httpx
import pytest

app_module = pytest.importorskip("app")
import slot_library  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg")


def _call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(_do())


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path / "output"))
    return tmp_path


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("v") / "loop.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=160x120:d=1:r=10", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    path], check=True)
    return path


def _post(name, content, ctype="video/mp4"):
    return _call("POST", "/api/slot-uploads", files={"file": (name, content, ctype)})


def test_text_file_named_mp4_is_rejected(lib):
    r = _post("fake.mp4", b"this is just text, not a video at all")
    assert r.status_code == 415
    assert slot_library.list_items(str(lib / "uploads/slot_library/local")) == []


def test_ftyp_header_without_a_video_stream_is_rejected(lib):
    r = _post("fake.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    assert r.status_code == 415


def test_extension_outside_allowlist_is_rejected(lib):
    assert _post("clip.exe", b"x").status_code == 415


def test_oversize_is_rejected(lib, monkeypatch):
    monkeypatch.setattr(app_module, "SLOT_UPLOAD_MAX_MB", 0)
    assert _post("a.mp4", b"x" * 10).status_code == 413


@needs_ffmpeg
def test_valid_upload_listed_and_path_traversal_name_stripped(lib, mp4):
    r = _post("../../evil/../name.mp4", open(mp4, "rb").read(), "application/octet-stream")
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["name"] == "name.mp4" and item["has_audio"] is True
    listed = _call("GET", "/api/slot-uploads").json()["items"]
    assert [i["id"] for i in listed] == [item["id"]]
    assert item["thumb"].startswith("/thumbnails/slot_local/")
    stored = os.listdir(lib / "uploads/slot_library/local")
    assert f"{item['id']}.mp4" in stored and not any(n.startswith(".tmp") for n in stored)
    assert not (lib / "evil").exists()


@needs_ffmpeg
def test_duration_cap(lib, mp4, monkeypatch):
    monkeypatch.setattr(app_module, "SLOT_UPLOAD_MAX_SECONDS", 0.2)
    assert _post("a.mp4", open(mp4, "rb").read()).status_code == 413


@needs_ffmpeg
def test_list_and_delete_are_scoped_by_owner(lib, mp4, monkeypatch):
    """Owners resolve to their own directory: B sees nothing of A and gets 404."""
    owner = {"id": "11111111-aaaa"}

    async def fake_owner(request):
        return owner["id"]
    monkeypatch.setattr(app_module, "_owner_id", fake_owner)
    item = _post("a.mp4", open(mp4, "rb").read()).json()
    owner["id"] = "22222222-bbbb"
    assert _call("GET", "/api/slot-uploads").json()["items"] == []
    assert _call("DELETE", f"/api/slot-uploads/{item['id']}").status_code == 404
    owner["id"] = "11111111-aaaa"
    assert len(_call("GET", "/api/slot-uploads").json()["items"]) == 1
    assert _call("DELETE", f"/api/slot-uploads/{item['id']}").status_code == 200
    assert _call("GET", "/api/slot-uploads").json()["items"] == []
    assert _call("DELETE", "/api/slot-uploads/not-an-id").status_code == 404


def test_account_purge_removes_the_library(lib):
    for base, rel in (("uploads", "slot_library/u-1"), ("thumbs", "slot_u-1"),
                      ("uploads", "slot_library/u-2")):
        os.makedirs(lib / base / rel)
        (lib / base / rel / "f").write_text("x")
    app_module._purge_local_jobs_for_user("u-1")
    assert not (lib / "uploads/slot_library/u-1").exists()
    assert not (lib / "thumbs/slot_u-1").exists()
    assert (lib / "uploads/slot_library/u-2/f").exists()
