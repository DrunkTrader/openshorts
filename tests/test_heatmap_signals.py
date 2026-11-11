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
    assert abs(hs.blend(100, 0.0) - 0.7) < 1e-9
