"""Layout picker and burned-subtitle probe on the pieces of a range job."""
import pytest

import burned_subtitles
import layout_picker
import range_ingest

PIECES = [{"file": f"T.piece_{i}.mp4", "start": 100.0 * i, "end": 100.0 * i + 30 + i}
          for i in range(1, 4)]


@pytest.fixture()
def sampled(monkeypatch):
    calls = []

    def fake(path, n=None, width=None):
        calls.append((path, n))
        return [f"{path}#{k}".encode() for k in range(n)]
    monkeypatch.setattr(layout_picker, "sample_frames", fake)
    return calls


@pytest.mark.parametrize("k", [1, 3, 12, 20])
def test_exactly_twelve_frames_each_from_a_listed_piece(sampled, k):
    paths = [f"p{i}.mp4" for i in range(k)]
    frames = layout_picker.sample_frames_pieces(paths)
    assert len(frames) == 12
    assert {p for p, _ in sampled} <= set(paths)
    if k <= 12:
        assert {p for p, _ in sampled} == set(paths)
        assert max(n for _, n in sampled) - min(n for _, n in sampled) <= 1


def test_twenty_pieces_are_spread_not_the_first_twelve(sampled):
    layout_picker.sample_frames_pieces([f"p{i}.mp4" for i in range(20)])
    assert sampled[-1][0] == "p18.mp4" and sampled[0][0] == "p0.mp4"


def test_width_stays_1024_and_prompt_is_the_shared_one():
    assert layout_picker.SAMPLE_FRAMES == 12 and layout_picker.SAMPLE_WIDTH == 1024
    import inspect
    assert "LAYOUT_CHOICE_PROMPT" in inspect.getsource(layout_picker.pick)  # one prompt, both paths


def test_pick_with_pieces_sends_pieces_frames_and_no_key_means_none(monkeypatch, sampled):
    monkeypatch.setattr(layout_picker, "ENABLED", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert layout_picker.pick(["a.mp4"], 30) == "none"
    assert sampled == []  # no key: not even frames are read


def _probe(monkeypatch, w=1920, h=1080, enabled=True, captions=True):
    seen = {"pick": [], "detect": []}
    monkeypatch.setattr(layout_picker, "ENABLED", enabled)
    monkeypatch.setattr(layout_picker, "pick_and_apply",
                        lambda paths, secs: seen["pick"].append((paths, secs)))
    monkeypatch.setattr(burned_subtitles, "detect",
                        lambda path: seen["detect"].append(path) or 0.8)
    band = range_ingest.probe_pieces("/job", PIECES, 9 / 16, lambda w, h, a: w / h <= a * 1.01,
                                     lambda p: (w, h), captions)
    return seen, band


def test_wide_pieces_run_picker_on_all_pieces_and_detect_on_the_largest(monkeypatch):
    seen, band = _probe(monkeypatch)
    assert seen["pick"] == [(["/job/" + p["file"] for p in PIECES], 31 + 32 + 33)]
    assert seen["detect"] == ["/job/T.piece_3.mp4"] and band == 0.8


def test_vertical_piece_skips_picker_and_subtitle_probe(monkeypatch):
    seen, band = _probe(monkeypatch, w=1080, h=1920)
    assert seen == {"pick": [], "detect": []} and band is None


def test_captions_off_skips_only_the_subtitle_probe(monkeypatch):
    seen, band = _probe(monkeypatch, captions=False)
    assert len(seen["pick"]) == 1 and seen["detect"] == [] and band is None


def test_picker_failure_does_not_stop_the_job(monkeypatch):
    seen, _ = _probe(monkeypatch)
    monkeypatch.setattr(layout_picker, "pick_and_apply", lambda *a: 1 / 0)
    assert range_ingest.probe_pieces("/job", PIECES, 9 / 16, lambda *a: False,
                                     lambda p: (1920, 1080), True) == 0.8


def test_apply_never_turns_an_explicit_choice_off(monkeypatch):
    monkeypatch.setattr(layout_picker, "ENABLED", True)
    import split_layout
    monkeypatch.setattr(split_layout, "ENABLED", True)
    layout_picker.apply("none")
    assert split_layout.ENABLED is True
