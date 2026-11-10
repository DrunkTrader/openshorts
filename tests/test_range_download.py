import os
import shutil

import pytest

import main
from test_audio_first import YT, fake_ydl  # noqa: F401  (fixture)
from ydl_fakes import FakeYDL

RANGES = [(0.0, 60.0), (100.0, 160.0), (300.0, 380.0)]


def _run(tmp_path, ranges=RANGES):
    return main._download_youtube_video(YT, str(tmp_path), ranges=ranges)


def _sections(fake, params):
    return list(params["download_ranges"]({}, None))


def test_ranges_passed_with_exact_cuts_and_numbered_names(fake_ydl, tmp_path):
    paths, title = _run(tmp_path)
    (dl,) = [i for i in fake_ydl.instances if "process_ie_result" in i.calls]
    assert [(c["start_time"], c["end_time"]) for c in _sections(fake_ydl, dl.params)] == RANGES
    assert dl.params["force_keyframes_at_cuts"] is True
    assert "%(section_number)s" in dl.params["outtmpl"]
    assert dl.params["noplaylist"] is True
    assert [os.path.basename(p) for p in paths] == [f"Talk.piece_{n}.mp4" for n in (1, 2, 3)]
    assert all(os.path.getsize(p) > 0 for p in paths)


def _format_used(fake):
    return [i.params["format"] for i in fake.instances if "process_ie_result" in i.calls][-1]


def test_format_is_1080p_off_the_paid_proxy(fake_ydl, tmp_path):
    _run(tmp_path)
    assert "height<=1080" in _format_used(fake_ydl) and "720" not in _format_used(fake_ydl)


def test_format_is_720p_on_the_paid_proxy(fake_ydl, tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://paid.invalid:2")

    def fail(params):
        if params.get("proxy") != "http://paid.invalid:2":
            raise RuntimeError("Sign in to confirm you're not a bot")
    fake_ydl.fail_extract = staticmethod(fail)
    _run(tmp_path)
    assert "height<=720" in _format_used(fake_ydl) and "1080" not in _format_used(fake_ydl)


def test_only_missing_ranges_are_retried(fake_ydl, tmp_path):
    fake_ydl.fail_sections = {2: 1}  # piece 2 fails once
    paths, _ = _run(tmp_path)
    assert all(paths)
    sessions = [i for i in fake_ydl.instances if "process_ie_result" in i.calls]
    assert len(sessions) == 2
    assert [c["index"] for c in _sections(fake_ydl, sessions[1].params)] == [2]


def test_piece_that_keeps_failing_is_dropped_not_fatal(fake_ydl, tmp_path):
    fake_ydl.fail_sections = {2: 5}
    paths, _ = _run(tmp_path)
    assert [p is None for p in paths] == [False, True, False]


def test_job_fails_only_when_every_piece_fails(fake_ydl, tmp_path):
    fake_ydl.fail_sections = {1: 99, 2: 99, 3: 99}
    with pytest.raises(RuntimeError, match="no range"):
        _run(tmp_path)


def test_existing_pieces_are_reused_for_the_same_ranges(fake_ydl, tmp_path):
    _run(tmp_path)
    fake_ydl.instances.clear()
    _run(tmp_path)
    assert not [i for i in fake_ydl.instances if "process_ie_result" in i.calls]
    _run(tmp_path, ranges=[(0.0, 60.0), (100.0, 170.0), (300.0, 380.0)])
    (dl,) = [i for i in fake_ydl.instances if "process_ie_result" in i.calls]
    assert [c["index"] for c in _sections(fake_ydl, dl.params)] == [2]


def test_stock_range_func_gives_no_section_index():
    # Why main yields its own sections: yt-dlp names a piece from the range's
    # `index`, which download_range_func never sets, so %(section_number)s
    # would be NA for every piece and each one would overwrite the last.
    from yt_dlp.utils import download_range_func
    (first,) = download_range_func(None, [(0, 5)])({"duration": 10}, None)
    assert "index" not in first


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_precise_cut_from_padded_piece(tmp_path):
    import subprocess
    from ffmpeg_utils import cut_clip
    # Luma encodes the source time as a 2 s sawtooth, so the first frame of a
    # cut says where in the source it starts. Keyframes only every 5 s.
    src = str(tmp_path / "src.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=60,"
                    "geq=lum='255*mod(T\\,2)/2':cb=128:cr=128",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
                    "-force_key_frames", "expr:gte(t,n_forced*5)", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-c:a", "aac", src], check=True)
    start, end, pad = 23.28, 41.68, 10.0
    piece_start = start - pad
    piece = str(tmp_path / "piece.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(piece_start),
                    "-to", str(end + pad), "-i", src, "-c:v", "libx264", "-c:a", "aac", piece],
                   check=True)
    out = str(tmp_path / "clip.mp4")
    cut_clip(piece, out, start - piece_start, end - piece_start, 1)

    def probe(args):
        return subprocess.check_output(["ffprobe", "-v", "error", *args], text=True).strip()
    assert abs(float(probe(["-show_entries", "format=duration", "-of", "csv=p=0", out]))
               - (end - start)) <= 0.1
    def luma(path, *seek):
        return subprocess.check_output(
            ["ffmpeg", "-v", "error", *seek, "-i", path, "-frames:v", "1",
             "-vf", "scale=1:1,format=gray", "-f", "rawvideo", "-"])[0]
    # One frame (0.04 s) is 5 luma levels of the 2 s sawtooth.
    assert abs(luma(out) - luma(src, "-ss", str(start))) <= 5
    assert abs(luma(out) - luma(src, "-ss", str(start + 0.5))) > 20  # the check can fail
