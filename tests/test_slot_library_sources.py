"""/api/slot-uploads url mode (server-side download through the job download
path, yt-dlp faked) and the import folder (SLOT_LIBRARY_IMPORT_DIR)."""
import asyncio
import os
import shutil
import subprocess

import httpx
import pytest

app_module = pytest.importorskip("app")
import slot_library  # noqa: E402
from test_audio_first import YT, fake_ydl  # noqa: E402,F401  (fixture)
from ydl_fakes import FakeYDL  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg")
PAID = "http://paid.invalid:2"


def _call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(_do())


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    import security_utils
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(app_module, "SLOT_LIBRARY_IMPORT_DIR", str(tmp_path / "import"))
    monkeypatch.setattr(security_utils, "assert_public_url", lambda u: None)  # no DNS
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.delenv("STATIC_PROXY_URLS", raising=False)
    (tmp_path / "import").mkdir()
    return tmp_path


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("v") / "loop.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=160x120:d=1:r=10", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    path], check=True)
    return path


def _items(lib, owner="local"):
    return slot_library.list_items(str(lib / "uploads/slot_library" / owner))


def _post_url(url, **extra):
    return _call("POST", "/api/slot-uploads", data={"url": url, **extra})


def _sessions():
    return [i for i in FakeYDL.instances if "process_ie_result" in i.calls]


# --- url mode -----------------------------------------------------------------
def test_non_video_url_refused_before_any_download(lib, fake_ydl):
    r = _post_url("https://www.youtube.com/playlist?list=PL123")
    assert r.status_code == 400 and "one video" in r.json()["detail"]
    assert FakeYDL.instances == []


def test_file_and_url_together_or_neither_refused(lib):
    assert _call("POST", "/api/slot-uploads", data={"name": "x"}).status_code == 400
    r = _call("POST", "/api/slot-uploads", data={"url": YT},
              files={"file": ("a.mp4", b"x", "video/mp4")})
    assert r.status_code == 400


@needs_ffmpeg
def test_url_download_is_range_capped_guarded_and_stored(lib, fake_ydl, mp4, monkeypatch):
    import ssrf_guard
    monkeypatch.setattr(app_module, "SLOT_UPLOAD_MAX_SECONDS", 120.0)
    guarded = []

    def write(self, info, section):
        guarded.append(ssrf_guard.active())
        name = self.params["outtmpl"].replace("%(ext)s", "mp4").replace(
            "%(section_number)s", str(section))
        shutil.copy(mp4, name)
        return name
    monkeypatch.setattr(FakeYDL, "_write", write)
    r = _post_url(YT, name="  Gameplay   loop ")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Gameplay loop"
    (dl,) = _sessions()
    assert list(dl.params["download_ranges"]({}, None)) == [
        {"start_time": 0.0, "end_time": 119.0, "index": 1}]
    assert dl.params["noplaylist"] is True
    assert guarded == [True]
    assert [i["id"] for i in _items(lib)] == [r.json()["id"]]
    left = os.listdir(lib / "uploads/slot_library/local")
    assert not any(n.startswith((".dl_", ".tmp_")) for n in left)


def test_downloaded_non_video_is_refused_and_cleaned(lib, fake_ydl):
    r = _post_url(YT)  # the fake writes 64 bytes of "x"
    assert r.status_code == 415
    assert _items(lib) == []
    assert not any(n.startswith(".dl_") for n in os.listdir(lib / "uploads/slot_library/local"))


def _fail_unless_paid(msg):
    def fail(params):
        if params.get("proxy") != PAID:
            raise RuntimeError(msg)
    return staticmethod(fail)


def _proxies():
    return [i.params.get("proxy") for i in FakeYDL.instances]


def test_paid_proxy_only_for_youtube_ip_failures(lib, fake_ydl, monkeypatch):
    monkeypatch.setenv("PROXY_URL", PAID)
    # A bot check another IP fixes: the paid route is tried.
    fake_ydl.fail_extract = _fail_unless_paid("Sign in to confirm you're not a bot")
    _post_url(YT)
    assert PAID in _proxies()
    # The first pass never carries the paid proxy.
    assert _proxies()[0] is None
    # A content block: no paid attempt.
    FakeYDL.instances = []
    fake_ydl.fail_extract = _fail_unless_paid("ERROR: Private video. Sign in if you've been granted access")
    assert _post_url(YT).status_code == 400
    assert FakeYDL.instances and PAID not in _proxies()
    # Not YouTube: never paid, whatever the error.
    FakeYDL.instances = []
    fake_ydl.fail_extract = _fail_unless_paid("Sign in to confirm you're not a bot")
    assert _post_url("https://cdn.example.com/v.mp4").status_code == 400
    assert FakeYDL.instances and PAID not in _proxies()
    assert len(FakeYDL.instances) == 1  # one direct attempt, no second (paid-allowed) pass


# --- import folder --------------------------------------------------------------
def _import():
    r = _call("POST", "/api/slot-uploads/import")
    assert r.status_code == 200, r.text
    return r.json()


@needs_ffmpeg
def test_import_moves_valid_keeps_invalid_and_ignores_symlinks(lib, mp4, tmp_path):
    imp = lib / "import"
    shutil.copy(mp4, imp / "Gameplay one.mp4")
    (imp / "broken.mp4").write_bytes(b"not a video")
    (imp / "notes.txt").write_text("ignored")
    outside = tmp_path / "secret.mp4"
    shutil.copy(mp4, outside)
    os.symlink(outside, imp / "link.mp4")
    (imp / "sub").mkdir()
    shutil.copy(mp4, imp / "sub" / "deep.mp4")
    out = _import()
    assert out == {"imported": ["Gameplay one.mp4"], "skipped": ["broken.mp4", "link.mp4"]}
    assert [i["name"] for i in _items(lib)] == ["Gameplay one"]
    assert not (imp / "Gameplay one.mp4").exists()
    assert (imp / "broken.mp4").exists() and (imp / "notes.txt").exists()
    assert os.path.islink(imp / "link.mp4") and outside.exists()
    assert (imp / "sub" / "deep.mp4").exists()
    assert _import() == {"imported": [], "skipped": ["broken.mp4", "link.mp4"]}  # not twice
    assert len(_items(lib)) == 1


def test_symlink_swapped_in_is_not_followed(lib, tmp_path, monkeypatch):
    """Even past the scandir check, the open refuses a symlink (O_NOFOLLOW)."""
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("no O_NOFOLLOW")
    target = tmp_path / "target.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    link = tmp_path / "l.mp4"
    os.symlink(target, link)
    with pytest.raises(OSError):
        app_module._slot_copy_nofollow(str(link), str(tmp_path / "out"), 1 << 20)
    assert not (tmp_path / "out").exists()


def test_import_folder_missing_is_a_noop(lib, monkeypatch):
    monkeypatch.setattr(app_module, "SLOT_LIBRARY_IMPORT_DIR", str(lib / "nope"))
    assert _import() == {"imported": [], "skipped": []}


@needs_ffmpeg
def test_import_goes_to_the_callers_library_only(lib, mp4, monkeypatch):
    async def owner_a(request):
        return "aaaa1111-owner"
    monkeypatch.setattr(app_module, "_owner_id", owner_a)
    shutil.copy(mp4, lib / "import" / "clip.mp4")
    assert _import()["imported"] == ["clip.mp4"]
    assert len(_items(lib, "aaaa1111-owner")) == 1 and _items(lib) == []


def test_import_refused_in_cloud(lib, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)

    async def owner(request):
        return "aaaa1111-owner"

    async def entitled(request):
        return None
    monkeypatch.setattr(app_module, "_owner_id", owner)
    monkeypatch.setattr(app_module, "require_managed_entitlement", entitled)
    monkeypatch.setattr(app_module, "_write_owner_marker", lambda d, o: None)
    assert _call("POST", "/api/slot-uploads/import").status_code == 404
