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


# --- through get_viral_clips: one pipeline for speech and non-speech windows ---
import json  # noqa: E402
import re  # noqa: E402


def _seg(a, b):
    return {"start": float(a), "end": float(b), "text": "so we keep talking about the plan here " * 2,
            "words": [{"word": "plan", "start": float(a), "end": a + 0.4}]}


def _mixed():
    """talk 0-300, silence 300-600, talk 600-900."""
    segs = [_seg(t, t + 10) for t in list(range(0, 300, 10)) + list(range(600, 900, 10))]
    return {"language": "en", "segments": segs}


def _curve(peaks):
    c = [0.05] * 900
    for t in peaks:
        for k in range(8):
            c[t + k] = 1.0 if k == 0 else 0.6
    return c


def _detail_clip(start, wid):
    return {"start": start, "end": start + 30, "source_window_id": wid, "predicted_score": 80,
            "video_description_for_tiktok": "d", "video_description_for_instagram": "d",
            "video_title_for_youtube_short": "t", "viral_hook_text": "h", "why": ""}


def _drive(main, monkeypatch, curve, detail=lambda ids: [], silent=True, tr=None, duration=900.0):
    calls = []

    def fake_stage(client, model, prompt, schema):
        gw = main.gemini_worker
        if schema is gw.SilentCopyResponse:
            n = prompt.count('"index"')
            calls.append(("copy", prompt))
            return {"shorts": [{"index": i, "viral_hook_text": "H", "video_title_for_youtube_short": "T",
                                "video_description_for_tiktok": "d", "video_description_for_instagram": "d"}
                               for i in range(n)]}, None
        if schema is gw.ScoreResponse:
            payload = json.loads(prompt.split("WINDOWS_JSON:")[-1].split("Return only:")[0])
            calls.append(("score", prompt))
            return {"windows": [{"id": w["id"], "start": 0, "end": 90, "score": 90 - n, "reason": "r"}
                                for n, w in enumerate(payload)]}, None
        wins = json.loads(prompt.split("CANDIDATE_WINDOWS_JSON:")[-1].split("Return only:")[0])
        ids = [i for w in wins for i in w.get("ids", [w["id"]])]
        calls.append(("detail", prompt))
        return {"shorts": detail(ids)}, None

    monkeypatch.setattr(main, "_run_gemini_stage", fake_stage)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(main.genai, "Client", lambda api_key=None: object())
    monkeypatch.setattr(main.silent_signals, "audio_energy_curve", lambda *a, **k: curve)
    out = main.get_viral_clips(tr or _mixed(), duration, silent={"audio": "a.m4a", "title": "Race", "chapters": []}
                               if silent else None)
    return (out or {}).get("shorts", []), calls


def test_global_top_n_merges_groups(main, monkeypatch, capsys):
    clips, _ = _drive(main, monkeypatch, _curve([310, 390, 470, 550]),
                      detail=lambda ids: [_detail_clip(0, ids[0])])
    out = capsys.readouterr().out
    m = re.search(r"Shortlist: (\d+) speech \+ (\d+) non-speech", out)
    # shortlist_target(900) = 10: the global top N, not "up to K per batch", and
    # neither group starves
    assert m and int(m.group(1)) + int(m.group(2)) == 10 and int(m.group(1)) > 0 and int(m.group(2)) > 0
    assert len(ss.merge_rankings(list(range(8)), list("abcd"), 10)) == 10
    assert len(ss.merge_rankings(list(range(3)), list("ab"), 10)) == 5
    assert any(c.get("kind") == "nonspeech" for c in clips)


def test_nonspeech_windows_never_sent_to_the_scoring_or_detail_prompts(main, monkeypatch):
    _, calls = _drive(main, monkeypatch, _curve([310, 390, 470, 550]))
    for kind, prompt in calls:
        if kind in ("score", "detail"):
            assert "window_ns" not in prompt
    assert [k for k, _ in calls].count("copy") == 1   # one batched call for every silent clip


def test_silent_stretch_without_signal_is_skipped(main, monkeypatch):
    clips, calls = _drive(main, monkeypatch, [0.0] * 900,
                          detail=lambda ids: [_detail_clip(0, ids[0])])
    assert not [c for c in clips if c.get("kind") == "nonspeech"]
    assert "copy" not in [k for k, _ in calls]


def test_min_clips_floor_enforced_in_code_with_nonspeech(main, monkeypatch):
    monkeypatch.setenv("CLIP_TARGET_MIN", "4")
    clips, calls = _drive(main, monkeypatch, _curve([310, 390, 470, 550]))
    silent_clips = [c for c in clips if c.get("kind") == "nonspeech"]
    # the detail pass returned nothing: the floor of 4 is met from the silent windows alone
    assert len(silent_clips) == 4 and len({c["source_window_id"] for c in silent_clips}) == 4
    assert all(c["video_title_for_youtube_short"] == "T" for c in silent_clips)


def test_local_llm_gets_only_a_text_prompt(main, monkeypatch):
    seen = []

    def fake_generate_json(*args, **kwargs):
        seen.append((args, kwargs))
        return {"shorts": []}, None

    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(main.llm_backend, "generate_json", fake_generate_json)
    main._run_gemini_stage(None, "m", "a text prompt", main.gemini_worker.SilentCopyResponse)
    args, kwargs = seen[0]
    assert isinstance(args[0], str) and set(kwargs) <= {"model"} and len(args) == 2


def test_get_viral_clips_holds_the_models_clips_to_their_bounds(main, monkeypatch):
    # B-1: the call to bound_clips itself, not only the helper
    def detail(ids):
        w = ids[0]
        return [_detail_clip(100, w) | {"end": 90.0},       # end before start
                _detail_clip(-20, w) | {"end": 30.0},       # before the video
                _detail_clip(850, w),                       # far outside its window
                _detail_clip(10, w)]
    clips, _ = _drive(main, monkeypatch, [0.0] * 900, detail=detail, silent=False)
    assert clips and all(0 <= c["start"] < c["end"] <= 900 for c in clips)
    assert not any(c["start"] == 100 and c["end"] == 90 for c in clips)
    assert max(c["end"] for c in clips) <= 150, "a clip far outside its window survived"


def test_undersized_nonspeech_group_below_the_floor_returns_what_exists(main, monkeypatch, capsys):
    # one 60 s silent stretch, nothing from the detail pass, floor 4: the one silent
    # clip is returned and the shortfall is logged, never padded
    monkeypatch.setenv("CLIP_TARGET_MIN", "4")
    segs = [_seg(t, t + 10) for t in list(range(0, 300, 10)) + list(range(360, 660, 10))]
    clips, _ = _drive(main, monkeypatch, _curve([310]), tr={"language": "en", "segments": segs}, duration=660.0)
    silent_clips = [c for c in clips if c.get("kind") == "nonspeech"]
    assert len(silent_clips) == 1 and len(clips) == 1
    assert "Floor of 4 not reached" in capsys.readouterr().out
