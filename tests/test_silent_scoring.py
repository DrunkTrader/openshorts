import silent_signals as ss


def _w(i, heat=None, energy=None, start=None, peak=None):
    start = i * 90.0 if start is None else start
    return {"id": f"window_ns_{i:03d}", "kind": "nonspeech", "start": start, "end": start + 90,
            "heat": heat, "audio_energy": energy, "peak": peak, "cut_density": None}


def test_percentile_within_group():
    ws = [_w(1, 0.1, 0.9), _w(2, 0.5, 0.5), _w(3, 0.9, 0.1)]
    ranked = ss.score_nonspeech(ws)
    pct = {w["id"]: w["score_pct"] for w in ranked}
    # heat weighs 0.6, energy 0.4: window 3 wins on heat, window 1 loses
    assert [w["id"] for w in ranked] == ["window_ns_003", "window_ns_002", "window_ns_001"]
    assert pct["window_ns_003"] > pct["window_ns_002"] > pct["window_ns_001"]
    assert all(0 <= v <= 1 for v in pct.values())
    # one signal only: that signal alone ranks
    only = ss.score_nonspeech([_w(1, None, 0.2), _w(2, None, 0.8)])
    assert [w["id"] for w in only] == ["window_ns_002", "window_ns_001"]


def test_no_signal_window_skipped():
    ws = [_w(1), _w(2, None, 0.0), _w(3, 0.4, None)]
    assert [w["id"] for w in ss.score_nonspeech(ws)] == ["window_ns_003"]
    assert ss.score_nonspeech([_w(1)]) == []


def test_cut_from_peak_bounds_15_60_and_peak_inside():
    curve = [0.1] * 300
    for t in range(100, 112):
        curve[t] = 1.0
    for peak in (5, 104.5, 130, 178):
        w = _w(1, 0.5, 0.5, start=100.0 if peak > 50 else 0.0, peak=peak)
        w["end"] = w["start"] + 90
        w["peak"] = max(w["start"], min(peak, w["end"]))
        c = ss.cut_from_peak(w, curve, 15, 60)
        assert 15 - 1e-6 <= c["end"] - c["start"] <= 60 + 1e-6, c
        assert w["start"] <= c["start"] <= w["peak"] <= c["end"] <= w["end"], (peak, c)
    # decay ends the clip 1.5 s after the energy drops, once the minimum is met
    w = _w(1, 0.5, 0.5, start=100.0, peak=105.5)
    c = ss.cut_from_peak(w, curve, 5, 60)
    assert abs(c["end"] - 113.5) < 1e-6
    assert ss.cut_from_peak(dict(w, end=w["start"] + 10), curve, 15, 60) is None
    assert c["video_title_for_youtube_short"] is None and c["source_window_id"] == w["id"]


def test_window_signals_from_energy_and_heat():
    ws = [_w(1, start=0.0), _w(2, start=90.0)]
    curve = [0.1] * 180
    curve[120] = 1.0
    ss.window_signals(ws, {"energy": curve, "heatmap": [{"start_time": 0, "end_time": 90, "value": 0.7}]})
    assert ws[0]["heat"] == 0.7 and ws[1]["heat"] is None
    assert ws[1]["peak"] == 120.5 and ws[1]["audio_energy"] > ws[0]["audio_energy"]
