import pytest

import source_ranges as sr


def clip(s, e, score=5):
    return {"start": s, "end": e, "predicted_score": score}


def test_pad_clamps_to_duration():
    assert sr.pad_ranges([clip(3, 40), clip(290, 298)], 300, pad=10) == [(0.0, 50.0), (280.0, 300.0)]


def test_merge_overlapping_and_adjacent():
    assert sr.merge_ranges([(0, 50), (40, 90), (95, 130)], gap=8) == [(0, 130)]


def test_merge_keeps_far_apart_ranges_separate():
    assert sr.merge_ranges([(100, 160), (0, 50)], gap=8) == [(0, 50), (100, 160)]


def test_assign_offsets_map_clip_inside_piece():
    shorts = [clip(100, 150), clip(20, 60), clip(70, 90)]
    merged = sr.merge_ranges(sr.pad_ranges(shorts, 300, pad=10), gap=8)
    assert merged == [(10.0, 160.0)]
    for c, (i, off) in zip(shorts, sr.assign(shorts, merged)):
        ps, pe = merged[i]
        assert off == ps and ps <= c["start"] and c["end"] <= pe


def test_total_cap_keeps_best_scored():
    shorts = [clip(0, 60, 9), clip(500, 560, 2), clip(1000, 1060, 8)]
    kept = sr.cap_total(shorts, 2000, pad=10, gap=8, max_total=200)
    assert kept == [shorts[0], shorts[2]]
    assert sr.cap_total(shorts, 2000, pad=10, gap=8, max_total=10_000) == shorts


def test_zero_length_and_inverted_clips_rejected():
    with pytest.raises(ValueError):
        sr.pad_ranges([clip(10, 10)], 100)
    with pytest.raises(ValueError):
        sr.pad_ranges([clip(20, 10)], 100)
    assert sr.usable([clip(10, 10), clip(20, 10), clip(1, 5), {"start": "x"}]) == [clip(1, 5)]
    assert sr.total_seconds([(0, 10), (20, 25)]) == 15
