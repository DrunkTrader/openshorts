"""Slot split (clip-level top/bottom of source or upload): pure builders, the
ffmpeg render, and how the seam captions follow the ratio."""
import json
import os
import shutil
import subprocess

import pytest

import gpu_render
import slot_split
import split_layout
import subtitles

UP = "a" * 32
UP2 = "b" * 32
DIMS = {UP: (1280, 720), UP2: (720, 1280)}
CENTRE = (960.0, 400.0)


def _spec(**over):
    payload = {"top": {"kind": "source"},
               "bottom": {"kind": "upload", "upload_id": UP, "offset_s": 1.5,
                          "fit": {"x": 0.25, "y": 0.75}},
               "ratio": "50/50", "audio": "source", "loop": True}
    payload.update(over)
    return slot_split.parse_slots(payload)


def test_parse_slots_validates():
    bad = [
        {},
        "x",
        {"top": {"kind": "weird"}},
        {"top": {"kind": "upload"}},
        {"top": {"kind": "upload", "upload_id": "../../etc/passwd"}},
        {"top": {"kind": "source"}, "ratio": "70/30"},
        {"top": {"kind": "source"}, "audio": "none"},
        {"top": {"kind": "source"}, "loop": "yes"},
        {"top": {"kind": "upload", "upload_id": UP, "offset_s": -1}},
        {"top": {"kind": "upload", "upload_id": UP, "offset_s": "x"}},
        {"top": {"kind": "upload", "upload_id": UP, "fit": {"x": 1.5}}},
    ]
    for payload in bad:
        with pytest.raises(slot_split.SlotError):
            slot_split.parse_slots(payload)
    ok = slot_split.parse_slots({"top": {"kind": "source"}})
    assert ok["bottom"] == {"kind": "source"} and ok["ratio"] == "50/50"
    assert ok["loop"] is True and ok["audio"] == "source"


def test_two_uploads_with_source_audio_use_upload_audio():
    s = slot_split.parse_slots({
        "top": {"kind": "upload", "upload_id": UP},
        "bottom": {"kind": "upload", "upload_id": UP2}})
    assert s["audio"] == "upload"
    assert slot_split.upload_ids(s) == [UP, UP2]


@pytest.mark.parametrize("ratio", ["50/50", "60/40"])
@pytest.mark.parametrize("kinds", [("source", "upload"), ("upload", "source"),
                                   ("upload", "upload"), ("source", "source")])
def test_graph_uses_only_supported_filters(ratio, kinds):
    slot = lambda k, i: ({"kind": "source"} if k == "source"
                         else {"kind": "upload", "upload_id": i})
    spec = slot_split.parse_slots({"top": slot(kinds[0], UP),
                                   "bottom": slot(kinds[1], UP2), "ratio": ratio})
    graph = slot_split.build_filtergraph(spec, 1920, 1080, 1080, 1920, DIMS, CENTRE)
    assert slot_split.filter_names(graph) <= {
        "crop", "scale", "pad", "vstack", "gblur", "overlay", "setsar",
        "format", "fps", "trim", "setpts", "split"}


@pytest.mark.parametrize("out_h", [1920, 1280, 854])
@pytest.mark.parametrize("ratio", ["50/50", "60/40"])
def test_heights_even_and_sum_to_out_h(ratio, out_h):
    top, bottom = slot_split.slot_heights(out_h, ratio)
    assert top % 2 == 0 and bottom % 2 == 0 and top + bottom == out_h


def test_upload_slot_has_loop_and_offset_args():
    spec = _spec()
    cmd = slot_split.build_command(
        spec, "cut.mp4", "out.mp4", {UP: "up.mp4"}, DIMS, 1920, 1080, 1080,
        1920, 6.0, CENTRE)
    i = cmd.index("up.mp4")
    assert cmd[i - 5:i + 1] == ["-stream_loop", "-1", "-ss", "1.5", "-i", "up.mp4"]
    assert cmd[1:4] == ["-y", "-i", "cut.mp4"]
    # fit x=0.25/y=0.75 lands in the crop offsets
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:v]scale=" in graph and ":x=" in graph
    no_loop = slot_split.input_args(_spec(loop=False), {UP: "up.mp4"})
    assert "-stream_loop" not in no_loop


def test_audio_modes_map_correctly():
    assert slot_split.audio_plan(_spec(audio="source")) == (None, "0:a?")
    assert slot_split.audio_plan(_spec(audio="upload")) == (None, "1:a")
    chain, label = slot_split.audio_plan(_spec(audio="mix"))
    assert "amix=inputs=2" in chain and label == "[a]"
    # no upload at all: nothing to mix with
    only_source = slot_split.parse_slots({"top": {"kind": "source"}, "audio": "mix"})
    assert slot_split.audio_plan(only_source) == (None, "0:a?")
    # a silent cut mixes down to just the upload
    assert slot_split.audio_plan(_spec(audio="mix"), source_has_audio=False) == (None, "1:a")


def test_seam_y_follows_ratio():
    assert slot_split.seam_fraction(1920, "50/50") == 0.5
    assert slot_split.seam_fraction(1920, "60/40") == 1152 / 1920 == 0.6
    assert slot_split.seam_range(5.0, 1920, "60/40") == [(0.0, 5.0, "split", None, 0.6)]


def test_seam_captions_are_pinned_at_the_ratio(tmp_path):
    import layout_ranges
    clip = str(tmp_path / "c.mp4")
    slot_split.write_layout(clip, 4.0, 1920, "60/40")
    seams = layout_ranges.split_ranges(layout_ranges.read(clip))
    assert seams == [(0.0, 4.0, 0.6)]
    transcript = {"segments": [{"start": 0, "end": 2, "text": "hi there", "words": [
        {"word": " hi", "start": 0.0, "end": 0.5},
        {"word": " there", "start": 0.5, "end": 1.0}]}]}
    out = str(tmp_path / "s.ass")
    assert subtitles.generate_ass(transcript, 0, 4, out, split_ranges=seams)
    text = open(out, encoding="utf-8-sig").read()
    assert "PlayResX: 162" in text
    assert "{\\an5\\pos(81,172.8)}" in text  # 0.6 * 288
    # mid-frame seam keeps the plain \an5 and no PlayResX
    slot_split.write_layout(clip, 4.0, 1920, "50/50")
    out2 = str(tmp_path / "s2.ass")
    subtitles.generate_ass(transcript, 0, 4, out2,
                           split_ranges=layout_ranges.split_ranges(layout_ranges.read(clip)))
    plain = open(out2, encoding="utf-8-sig").read()
    assert "{\\an5}" in plain and "PlayResX" not in plain and "\\pos" not in plain


def test_seam_survives_recut_remap():
    import layout_ranges
    ranges = layout_ranges.normalise([(0, 10, "split", None, 0.6)])
    moved = layout_ranges.remap(ranges, [{"start": 2, "end": 4}, {"start": 6, "end": 8}])
    assert [r["seam"] for r in moved] == [0.6, 0.6]
    assert layout_ranges.split_ranges(moved)[0] == (0.0, 2.0, 0.6)


def test_both_source_slots_equal_existing_split_geometry():
    spec = slot_split.parse_slots({"top": {"kind": "source"},
                                   "bottom": {"kind": "source"}})
    ours = slot_split.build_filtergraph(spec, 1920, 1080, 1080, 1920, {}, CENTRE)
    assert ours == split_layout.split_filtergraph(1920, 1080, 1080, 1920, CENTRE, CENTRE)


def test_upload_graph_is_unreadable_by_the_gpu_renderer():
    # Conscious fallback: an extra-input graph raises Unsupported, so the
    # render goes through ffmpeg (slot_split.build_command is that path).
    mixed = slot_split.build_filtergraph(_spec(), 1920, 1080, 1080, 1920, DIMS, CENTRE)
    with pytest.raises(gpu_render.Unsupported):
        gpu_render.layers_from_graph(mixed, 1920, 1080, 1080, 1920)
    both = slot_split.parse_slots({"top": {"kind": "source"}, "bottom": {"kind": "source"}})
    graph = slot_split.build_filtergraph(both, 1920, 1080, 1080, 1920, {}, CENTRE)
    assert gpu_render.layers_from_graph(graph, 1920, 1080, 1080, 1920)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("ratio", ["50/50", "60/40"])
def test_ffmpeg_render_loops_a_short_upload(tmp_path, ratio):
    def make(name, seconds, size, tone):
        path = str(tmp_path / name)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"testsrc2=s={size}:d={seconds}:r=25", "-f", "lavfi", "-i",
             f"sine=f={tone}:d={seconds}", "-c:v", "libx264", "-preset",
             "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
            check=True)
        return path

    cut = make("cut.mp4", 6, "640x360", 440)
    up = make("up.mp4", 2, "320x240", 880)
    spec = slot_split.parse_slots({
        "top": {"kind": "source"},
        "bottom": {"kind": "upload", "upload_id": UP, "offset_s": 0.5},
        "ratio": ratio, "audio": "source"})
    out = str(tmp_path / "out.mp4")
    cmd = slot_split.build_command(
        spec, cut, out, {UP: up}, {UP: (320, 240)}, 640, 360, 360, 640, 6.0,
        (320.0, 150.0))
    slot_split.run_ffmpeg(cmd)
    w, h, duration, has_audio = slot_split.probe_video(out)
    assert (w, h) == (360, 640)
    assert abs(duration - 6.0) <= 0.1
    assert has_audio
