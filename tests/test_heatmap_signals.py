import heatmap_signals as hs

W = [{"id": "a", "start": 0, "end": 10}, {"id": "b", "start": 10, "end": 20},
     {"id": "c", "start": 20, "end": 30}]
HM = [{"start_time": 0, "end_time": 5, "value": 0.2}, {"start_time": 5, "end_time": 10, "value": 0.6},
      {"start_time": 10, "end_time": 30, "value": 1.0}]


def test_window_replay_time_weighted():
    r = hs.window_replay(W, HM)
    assert abs(r["a"]["heat"] - 0.4) < 1e-9
    assert r["a"]["peak"] == 0.6 and r["a"]["peak_at"] == 7.5
    assert r["b"]["heat"] == 1.0
    assert r["a"]["heat_pct"] < r["b"]["heat_pct"]
    # a window straddling two buckets weighs each by its overlap
    assert abs(hs.window_replay([{"id": "x", "start": 4, "end": 8}], HM)["x"]["heat"] - (0.2 + 3 * 0.6) / 4) < 1e-9


def test_handles_missing_or_malformed_heatmap():
    for bad in (None, [], "x", [1, None], [{"start_time": 0}], [{"start_time": 5, "end_time": 1, "value": 1}]):
        assert hs.window_replay(W, bad) == {}
    assert hs.normalise([{"start_time": 0, "end_time": 1, "value": 7}])[0]["value"] == 1.0
    assert hs.normalise([{"start_time": 0, "end_time": 1, "value": float("nan")}]) == []


def _scored():
    return [{"id": "a", "score": 80}, {"id": "b", "score": 78}, {"id": "c", "score": 50}]


def test_blend_changes_order_only_with_coverage():
    full = {"a": {"heat_pct": 0.1}, "b": {"heat_pct": 0.9}, "c": {"heat_pct": 0.5}}
    assert [s["id"] for s in hs.rank_scored(_scored(), full, 3, 0.3)] == ["b", "a", "c"]
    thin = {"b": {"heat_pct": 0.9}}  # 1 of 3 windows: below 50%
    assert [s["id"] for s in hs.rank_scored(_scored(), thin, 3, 0.3)] == ["a", "b", "c"]
    half = {"a": {"heat_pct": 0.1}, "b": {"heat_pct": 0.9}}  # 2 of 4 counts
    assert [s["id"] for s in hs.rank_scored(_scored(), half, 4, 0.3)][0] == "b"


def test_blend_zero_weight_is_identity(monkeypatch):
    full = {"a": {"heat_pct": 0.0}, "b": {"heat_pct": 1.0}, "c": {"heat_pct": 1.0}}
    assert [s["id"] for s in hs.rank_scored(_scored(), full, 3, 0)] == ["a", "b", "c"]
    monkeypatch.setenv("REPLAY_HEAT_WEIGHT", "0")
    assert [s["id"] for s in hs.rank_scored(_scored(), full, 3)] == ["a", "b", "c"]
    assert hs.blend(80, 1.0, 0) == 0.8
    monkeypatch.delenv("REPLAY_HEAT_WEIGHT")
    assert abs(hs.blend(100, 0.0) - 0.85) < 1e-9
    assert hs.blend(80, None) == 0.8


def test_prompt_contains_replay_rule_and_template_formats():
    import gemini_worker as gw
    out = gw.SCORE_PROMPT_TEMPLATE.format(video_duration=1, language="en", windows_json="[]")
    assert "replay_heat" in out and "replay_peak" in out


def test_payload_attaches_fields_only_when_present():
    from clip_selection import build_window_payload
    ws = [{"id": "a", "start": 0, "end": 10, "text": "t"}, {"id": "b", "start": 10, "end": 20, "text": "u"}]
    out = build_window_payload(ws, None, {"a": {"heat": 0.456, "peak": 0.9}})
    assert out[0]["replay_heat"] == 0.46 and out[0]["replay_peak"] == 0.9
    assert "replay_heat" not in out[1] and "viewers_clipped" not in out[1]
    out = build_window_payload(ws, {"b": {"clips": 2, "views": 9, "spans": [[1, 2]]}}, None)
    assert out[1]["viewers_clipped"] == 2 and out[1]["viewer_moments"] == [[1, 2]]
    assert set(out[0]) == {"id", "start", "end", "text"}


def _shortlist(main, monkeypatch, replay_heat):
    import json
    seen = []

    def fake_stage(client, model, prompt, schema):
        if schema is main.gemini_worker.ScoreResponse:
            ws = json.loads(prompt.split("WINDOWS_JSON:")[-1].split("Return only:")[0])
            return {"windows": [{"id": w["id"], "start": 0, "end": 1, "score": 90 - int(w["id"][-3:]),
                                 "reason": "r"} for w in ws]}, None
        ws = json.loads(prompt.split("CANDIDATE_WINDOWS_JSON:")[-1].split("Return only:")[0])
        seen.extend(w["id"] for w in ws)
        return {"shorts": []}, None

    monkeypatch.setattr(main, "_run_gemini_stage", fake_stage)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(main.genai, "Client", lambda api_key=None: object())
    tr = {"language": "en", "segments": [
        {"start": i * 15.0, "end": i * 15.0 + 15, "text": f"s {i}",
         "words": [{"word": "w", "start": i * 15.0, "end": i * 15.0 + 1}]} for i in range(40)]}
    main.get_viral_clips(tr, 600.0, replay_heat=replay_heat)
    return seen


def test_get_viral_clips_blend_reorders_shortlist(main, monkeypatch):
    base = _shortlist(main, monkeypatch, None)
    hot_tail = [{"start_time": 0, "end_time": 450, "value": 0.0}, {"start_time": 450, "end_time": 600, "value": 1.0}]
    heated = _shortlist(main, monkeypatch, hot_tail)
    assert set(heated) != set(base)
    monkeypatch.setenv("REPLAY_HEAT_WEIGHT", "0")
    assert _shortlist(main, monkeypatch, hot_tail) == base


def test_uncovered_windows_keep_their_pure_score():
    # 3 of 5 windows covered, all with a middling heat percentile: equal scores
    # must not let the uncovered ones outrank the covered ones.
    scored = [{"id": f"w{i}", "score": 60} for i in range(5)]
    replay = {f"w{i}": {"heat_pct": 0.5} for i in range(3)}
    assert [s["id"] for s in hs.rank_scored(scored, replay, 5, 0.3)] == [f"w{i}" for i in range(5)]
