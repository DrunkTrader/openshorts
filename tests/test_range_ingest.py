"""The post-pick half of a range job (light: no main, no network)."""
import os

import pytest

import range_ingest
import source_ranges as sr


def clip(s, e, score=5):
    return {"start": s, "end": e, "predicted_score": score}


def fake_download(missing=()):
    calls = []

    def download(merged):
        calls.append(merged)
        return [None if n in missing else f"/job/T.piece_{n + 1}.mp4" for n in range(len(merged))]
    download.calls = calls
    return download


def test_offsets_are_piece_starts_and_clips_keep_source_time():
    shorts = [clip(100, 130), clip(400, 440)]
    dl = fake_download()
    kept, piece_for, pieces = range_ingest.plan_pieces(shorts, 600, dl)
    assert kept == shorts and kept[0]["start"] == 100          # absolute
    assert piece_for == {0: ("/job/T.piece_1.mp4", 90.0), 1: ("/job/T.piece_2.mp4", 390.0)}
    assert pieces == [{"file": "T.piece_1.mp4", "start": 90.0, "end": 140.0},
                      {"file": "T.piece_2.mp4", "start": 390.0, "end": 450.0}]
    assert dl.calls == [[(90.0, 140.0), (390.0, 450.0)]]


def test_clip_in_a_failed_piece_is_dropped_and_indices_close_up():
    kept, piece_for, pieces = range_ingest.plan_pieces(
        [clip(100, 130), clip(400, 440), clip(500, 540)], 600, fake_download(missing={1}))
    assert [c["start"] for c in kept] == [100, 500]
    assert list(piece_for) == [0, 1] and piece_for[1][0] == "/job/T.piece_3.mp4" and len(pieces) == 2


def test_no_surviving_clip_is_an_error():
    with pytest.raises(RuntimeError, match="No range"):
        range_ingest.plan_pieces([clip(100, 130)], 600, fake_download(missing={0}))
    with pytest.raises(RuntimeError, match="usable length"):
        range_ingest.plan_pieces([clip(10, 10)], 600, fake_download())


def test_clip_at_or_past_the_duration_is_dropped_and_a_long_end_clamped():
    shorts = [clip(100, 130), clip(600, 640), clip(700, 720), clip(580, 650)]
    kept, _, _ = range_ingest.plan_pieces(shorts, 600, fake_download())
    assert [(c["start"], c["end"]) for c in kept] == [(100, 130), (580, 600)]


def test_cap_never_drops_below_the_floor_and_says_so(capsys):
    shorts = [clip(0, 60, 9), clip(1000, 1060, 2), clip(2000, 2060, 5)]
    assert len(sr.cap_total(shorts, 3000, max_total=100, min_keep=1)) == 1
    kept = sr.cap_total(shorts, 3000, max_total=100, min_keep=3)
    assert len(kept) == 3 and "floor" in capsys.readouterr().out


def test_failed_pieces_below_the_floor_are_logged(capsys):
    range_ingest.plan_pieces([clip(100, 130), clip(400, 440)], 600, fake_download(missing={1}), min_keep=2)
    assert "below the floor" in capsys.readouterr().out


def test_cleanup_removes_audio_and_sidecar_and_keeps_pieces_for_recut(tmp_path):
    for n in ("T.audio.m4a", "T.piece_1.mp4", "T.pieces.json"):
        (tmp_path / n).write_text("x")
    pieces = [{"file": "T.piece_1.mp4", "start": 0, "end": 5}]
    range_ingest.cleanup(str(tmp_path), str(tmp_path / "T.audio.m4a"), pieces, keep_source=True)
    assert sorted(os.listdir(tmp_path)) == ["T.piece_1.mp4"]
    (tmp_path / "T.pieces.json").write_text("x")
    range_ingest.cleanup(str(tmp_path), None, pieces, keep_source=False)
    assert os.listdir(tmp_path) == []


def test_record_source_keys_for_a_range_job():
    meta = {}
    pieces = [{"file": "T.piece_1.mp4", "start": 90.0, "end": 140.0},
              {"file": "T.piece_2.mp4", "start": 390.0, "end": 450.0}]
    range_ingest.record_source(meta, "/job/T.audio.m4a", pieces, "https://y.example/v", 600.0, range_mode=True)
    assert meta == {"source_video": "T.piece_1.mp4", "source_pieces": pieces,
                    "source_url": "https://y.example/v", "source_duration": 600.0}


def test_record_source_for_a_whole_video_adds_nothing_else():
    meta = {}
    range_ingest.record_source(meta, "/job/Talk.mp4", [])
    assert meta == {"source_video": "Talk.mp4"}


def test_cleanup_is_called_with_audio_sidecar_and_keeps_pieces(tmp_path):
    for name in ("T.audio.m4a", "T.pieces.json", "T.piece_1.mp4"):
        (tmp_path / name).write_bytes(b"x")
    range_ingest.cleanup(str(tmp_path), str(tmp_path / "T.audio.m4a"),
                         [{"file": "T.piece_1.mp4"}], keep_source=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["T.piece_1.mp4"]


def test_range_mode_records_url_and_duration_even_without_pieces():
    meta = {}
    range_ingest.record_source(meta, "/job/T.audio.m4a", [], "https://y.example/v", 600.0, range_mode=True)
    assert meta["source_url"] == "https://y.example/v" and meta["source_duration"] == 600.0
    assert "source_pieces" not in meta
