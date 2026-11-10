import argparse
import os

import pytest

from ydl_fakes import FakeYDL

YT = "https://www.youtube.com/watch?v=abc123"


def _args(**kw):
    base = dict(url=YT, input=None, skip_analysis=False, transcript=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize("args,env,billing,expected", [
    (_args(), {}, False, True),
    (_args(), {"RANGE_DOWNLOAD": "1"}, False, True),
    (_args(), {}, True, False),
    (_args(), {"RANGE_DOWNLOAD": "0"}, False, False),
    (_args(skip_analysis=True), {}, False, False),
    (_args(transcript="t.json"), {}, False, False),
    (_args(), {"MAX_SOURCE_MINUTES": "10"}, False, False),
    (_args(), {"SOURCE_CAP_MINUTES": "60"}, False, False),
    (_args(url=None, input="a.mp4"), {}, False, False),
    (_args(url="https://cdn.example.com/video.mp4"), {}, False, False),
])
def test_range_mode_gate(main, args, env, billing, expected):
    assert main.use_range_download(args, env, billing) is expected


@pytest.fixture
def fake_ydl(main, monkeypatch, tmp_path):
    import security_utils
    import ssrf_guard
    FakeYDL.instances = []
    FakeYDL.fail_extract = staticmethod(lambda params: None)
    FakeYDL.fail_sections = {}
    FakeYDL.info = {"id": "abc123", "title": "Talk", "duration": 300, "heatmap": [{"value": 1}],
                    "chapters": [{"title": "Intro"}], "extractor_key": "Youtube",
                    "formats": [{"format_id": "1"}], "_type": "video"}
    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(security_utils, "assert_public_url", lambda u: None)
    monkeypatch.setattr(ssrf_guard, "assert_public_urls", lambda i: None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setenv("COOKIES_FILE", str(tmp_path / "c.txt"))
    return FakeYDL


def _downloads(fake):
    return [i for i in fake.instances if "process_ie_result" in i.calls]


def test_audio_only_opts(main, fake_ydl, tmp_path):
    from yt_clients import hd_extractor_args, fallback_extractor_args
    path, title = main._download_youtube_video(YT, str(tmp_path), audio_only=True)
    (dl,) = _downloads(fake_ydl)
    assert dl.params["format"] == "bestaudio[ext=m4a]/bestaudio"
    assert "merge_output_format" not in dl.params
    assert dl.params["noplaylist"] is True
    assert dl.params["extractor_args"] in (hd_extractor_args("", ""), fallback_extractor_args("", ""))
    assert dl.params["outtmpl"].endswith("Talk.audio.%(ext)s")
    assert path.endswith("Talk.audio.m4a") and os.path.exists(path) and title == "Talk"


def test_meta_out_filled_from_probe(main, fake_ydl, tmp_path):
    meta = {}
    main._download_youtube_video(YT, str(tmp_path), audio_only=True, meta_out=meta)
    assert meta == {"duration": 300, "title": "Talk", "heatmap": [{"value": 1}],
                    "chapters": [{"title": "Intro"}], "extractor": "Youtube", "id": "abc123"}


def test_audio_first_never_requests_video_format(main, fake_ydl, tmp_path):
    main._download_youtube_video(YT, str(tmp_path), audio_only=True)
    assert all("bestvideo" not in str(i.params.get("format", "")) for i in fake_ydl.instances)


def _proxies_until_paid(main, fake_ydl, tmp_path, monkeypatch, url):
    monkeypatch.setenv("STATIC_PROXY_URLS", "http://static.invalid:1")
    monkeypatch.setenv("PROXY_URL", "http://paid.invalid:2")
    monkeypatch.setenv("DIRECT_FIRST", "1")
    monkeypatch.setenv("YOUTUBE_COOKIES", "# Netscape HTTP Cookie File")
    monkeypatch.setenv("BGUTIL_BASE_URL", "http://bgutil.invalid:3")

    def fail(params):
        if params.get("proxy") != "http://paid.invalid:2":
            raise RuntimeError("Sign in to confirm you're not a bot")
    fake_ydl.fail_extract = staticmethod(fail)
    try:
        main._download_youtube_video(url, str(tmp_path), audio_only=True)
    except RuntimeError:
        pass
    return [i.params.get("proxy") for i in fake_ydl.instances if i.calls == ["extract_info"]
            or "process_ie_result" in i.calls]


def test_proxy_routing_unchanged_for_audio(main, fake_ydl, tmp_path, monkeypatch):
    seen = _proxies_until_paid(main, fake_ydl, tmp_path, monkeypatch, YT)
    assert seen[0] is None and seen[1] == "http://static.invalid:1"
    assert seen[-1] == "http://paid.invalid:2"
    assert [p for p in seen if p == "http://paid.invalid:2"]


def test_non_youtube_audio_never_uses_paid_proxy(main, fake_ydl, tmp_path, monkeypatch):
    seen = _proxies_until_paid(main, fake_ydl, tmp_path, monkeypatch, "https://cdn.example.com/v.mp4")
    assert "http://paid.invalid:2" not in seen


def test_release_models_called_after_audio_transcription(main, monkeypatch, tmp_path):
    import transcribe_backends
    calls = []
    monkeypatch.setattr(transcribe_backends, "release_models", lambda: calls.append("release"))
    monkeypatch.setattr(transcribe_backends, "transcribe_media",
                        lambda p: {"language": "en", "segments": []})
    audio = tmp_path / "Talk.audio.m4a"
    audio.write_bytes(b"x")
    main.transcribe_video(str(audio))
    assert calls == ["release"]
