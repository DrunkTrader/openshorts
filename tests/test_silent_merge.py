"""merge_rankings: one global ranking, and a small group is not starved by it."""
import clip_selection as cs
import silent_signals as ss


def _sp(n):
    return [{"id": f"window_{i:03d}", "score": 90 - i} for i in range(n)]


def _ns(n, strong=True):
    return [{"id": f"window_ns_{i:03d}", "peak_energy": 1.0 if strong else 0.1, "heat": None} for i in range(n)]


def _merge(sp, ns, n=10, bar=0.9):
    return ss.merge_rankings(sp, ns, n, speech_ok=lambda w: w["score"] >= 50,
                             silent_ok=lambda w: ss.passes_bar(w, bar))


def _kinds(out):
    return sum(1 for w in out if "ns" in w["id"]), sum(1 for w in out if "ns" not in w["id"])


def test_one_strong_nonspeech_window_among_20_speech_gets_a_slot():
    out = _merge(_sp(20), _ns(1))
    assert len(out) == 10 and _kinds(out) == (1, 9)


def test_three_nonspeech_against_20_speech_get_their_share():
    out = _merge(_sp(20), _ns(3))
    assert len(out) == 10 and _kinds(out)[0] >= 1


def test_three_speech_against_30_nonspeech_keep_a_speech_slot():
    out = _merge(_sp(3), _ns(30))
    assert len(out) == 10 and _kinds(out)[1] >= 1


def test_a_group_with_nothing_above_its_bar_gets_no_reserved_slot():
    out = _merge(_sp(20), _ns(1, strong=False))
    assert len(out) == 10 and _kinds(out) == (0, 10)   # percentile alone: a lone weak window loses


def test_shortlist_is_the_target_when_there_are_enough_windows_and_all_when_not():
    assert len(_merge(_sp(8), _ns(4))) == 10
    assert len(_merge(_sp(3), _ns(2))) == 5


def test_rank_score_is_set_on_every_window_and_follows_the_merged_order():
    sp, ns = _sp(20), _ns(3)
    out = _merge(sp, ns)
    assert all(0 <= w["rank_score"] <= 1 for w in sp + ns)
    scores = [w["rank_score"] for w in out]
    assert scores == sorted(scores, reverse=True)
    assert min(scores) >= max(w["rank_score"] for w in sp + ns if w not in out)


def test_energy_bar_uses_the_whole_curve():
    curve = [0.1] * 90 + [1.0] * 10
    assert ss.energy_bar(curve, 90) == 1.0 and ss.energy_bar([], 90) is None
    assert ss.passes_bar({"peak_energy": 1.0}, 1.0) and not ss.passes_bar({"peak_energy": 0.5}, 1.0)
    assert ss.passes_bar({"heat": 0.6}, None) and not ss.passes_bar({}, 1.0)


def test_cross_group_dedupe_and_trim_use_rank_score_not_predicted_score():
    speech = {"start": 100, "end": 130, "predicted_score": 90, "rank_score": 0.3, "source_window_id": "window_001"}
    silent = {"start": 105, "end": 135, "predicted_score": 12, "rank_score": 0.8, "source_window_id": "window_ns_001"}
    assert cs.dedupe_overlapping([speech, silent]) == [silent]
    assert cs.dedupe_overlapping([dict(speech, rank_score=0.9), silent]) == [dict(speech, rank_score=0.9)]
    other = {"start": 500, "end": 530, "predicted_score": 80, "rank_score": 0.5}
    assert cs.trim_to_best([speech, silent, other], 2) == [silent, other]
    # no rank_score anywhere: the model's score decides, as before
    plain = [{"start": 0, "end": 30, "predicted_score": 40}, {"start": 10, "end": 40, "predicted_score": 80}]
    assert cs.dedupe_overlapping(plain) == [plain[1]]


def test_clip_with_an_unknown_window_id_is_held_to_the_nearest_window():
    items = [{"id": "window_001", "start": 0.0, "end": 100.0}, {"id": "window_002", "start": 300.0, "end": 400.0}]
    kept, notes = cs.bound_clips([{"start": 250.0, "end": 330.0, "source_window_id": "nope"}], items, 500.0, 15, 60)
    assert len(kept) == 1 and kept[0]["start"] >= 300.0 and kept[0]["end"] <= 400.0
    assert any("unknown window id" in n for n in notes)
    dropped, _ = cs.bound_clips([{"start": 250.0, "end": 290.0, "source_window_id": "nope"}], items, 500.0, 15, 60)
    assert dropped == []   # nothing of it inside the nearest window
