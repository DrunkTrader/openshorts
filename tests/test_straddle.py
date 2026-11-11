import json

import clip_selection as cs


def _tr(n=30, step=15.0):
    return {"language": "en", "segments": [
        {"start": i * step, "end": (i + 1) * step, "text": f"line{i:02d} " + "x" * 40,
         "words": [{"word": f"w{i}", "start": i * step, "end": i * step + 1}]} for i in range(n)]}


def _windows(tr):
    return cs.build_transcript_windows(tr, 450.0, window_seconds=90)


def test_merge_adjacent_windows_dedupes_overlap_text():
    tr = _tr()
    ws = _windows(tr)
    merged = cs.merge_adjacent([ws[0], ws[1]], ws, tr, max_span=300)
    assert len(merged) == 1 and merged[0]["id"] == ws[0]["id"] and merged[0]["ids"] == [ws[0]["id"], ws[1]["id"]]
    assert merged[0]["start"] == ws[0]["start"] and merged[0]["end"] == ws[1]["end"]
    text = merged[0]["text"]
    for i in range(int(merged[0]["end"] // 15)):
        assert text.count(f"line{i:02d} ") == 1, i  # the 30 s overlap is counted once
    assert text.count("line") == int((merged[0]["end"] - merged[0]["start"]) // 15)


def test_non_adjacent_not_merged():
    tr = _tr()
    ws = _windows(tr)
    out = cs.merge_adjacent([ws[0], ws[2]], ws, tr, max_span=1000)
    assert [w["id"] for w in out] == [ws[0]["id"], ws[2]["id"]]
    assert all("ids" not in w for w in out)
    # adjacent but over the span cap: stays apart; order follows the shortlist rank
    out = cs.merge_adjacent([ws[2], ws[1]], ws, tr, max_span=100)
    assert [w["id"] for w in out] == [ws[2]["id"], ws[1]["id"]]


def test_neighbour_text_trimmed_to_budget():
    tr = _tr()
    ws = _windows(tr)
    item = cs.neighbour_context([ws[2]], tr, max_chars=100)[0]
    assert 0 < len(item["context_before"]) <= 100 and 0 < len(item["context_after"]) <= 100
    assert item["search_start"] < ws[2]["start"] and item["search_end"] > ws[2]["end"]
    # the trimmed text is exactly the lines the opened seconds cover
    assert item["context_before"].count("line") == round((ws[2]["start"] - item["search_start"]) / 15)
    first = cs.neighbour_context([ws[0]], tr, max_chars=100)[0]
    assert first["context_before"] == "" and first["search_start"] == ws[0]["start"]
    assert cs.neighbour_context([ws[2]], tr, max_chars=0)[0]["context_after"] == ""


def test_clip_spanning_two_windows_survives_snap():
    tr = _tr()
    ws = _windows(tr)
    seam = ws[1]["start"] + 20  # 45 s moment across the seam of window 1 and 2
    words = [{"w": f"w{i}", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(int(450 / 0.5))]
    start, end = seam - 20.0, seam + 25.0
    assert start < ws[0]["end"] and end > ws[1]["start"]
    ns, ne = cs.snap_clip_to_words(start, end, words, 450.0, min_duration=15, max_duration=60)
    assert abs(ns - start) <= 1.5 and abs(ne - end) <= 1.5 and ne - ns <= 60
    kept = cs.dedupe_overlapping([{"start": ns, "end": ne, "predicted_score": 80, "source_window_id": ws[0]["id"]}])
    assert len(kept) == 1


def test_prompt_size_under_budget():
    import gemini_worker as gw
    tr = {"language": "en", "segments": [
        {"start": i * 15.0, "end": (i + 1) * 15.0, "text": "palabra " * 12} for i in range(120)]}
    ws = cs.build_transcript_windows(tr, 1800.0, window_seconds=135)
    items = cs.neighbour_context(cs.merge_adjacent(ws[:6], ws, tr, max_span=270), tr, 600)
    for batch in cs.detail_batches(items, 9000):
        prompt = gw.DETAIL_PROMPT_TEMPLATE.format(
            video_duration=1800, language="en", min_clips=2, max_clips=6, min_secs=15, max_secs=60,
            windows_json=json.dumps(cs.build_window_payload(batch), ensure_ascii=False))
        assert len(prompt) / 3.5 < 4000, len(prompt)


def test_prompt_allows_context_but_bounds_clip():
    import gemini_worker as gw
    t = gw.DETAIL_PROMPT_TEMPLATE
    assert "Stay within the candidate window boundaries" not in t
    assert "context_before" in t and "context_after" in t
    assert "search_start" in t and "search_end" in t and "must start and end within" in t
    assert "THE 2-SECOND RULE" in t and "STANDS ALONE" in t
    t.format(video_duration=1, language="en", min_clips=1, max_clips=2, min_secs=15, max_secs=60,
             windows_json="[]")


def test_get_viral_clips_sends_context_and_merged_window(main, monkeypatch):
    seen = []

    def fake_stage(client, model, prompt, schema):
        if schema is main.gemini_worker.ScoreResponse:
            ws = json.loads(prompt.split("WINDOWS_JSON:")[-1].split("Return only:")[0])
            return {"windows": [{"id": w["id"], "start": 0, "end": 1, "score": 90, "reason": "r"} for w in ws]}, None
        seen.extend(json.loads(prompt.split("CANDIDATE_WINDOWS_JSON:")[-1].split("Return only:")[0]))
        return {"shorts": []}, None

    monkeypatch.setattr(main, "_run_gemini_stage", fake_stage)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(main.genai, "Client", lambda api_key=None: object())
    main.get_viral_clips(_tr(40), 600.0)
    assert any(w.get("ids") for w in seen)
    assert all("search_start" in w and w["search_start"] <= w["start"] <= w["end"] <= w["search_end"] for w in seen)


def _bound(shorts, items=None, dur=600.0):
    items = items if items is not None else [
        {"id": "window_001", "ids": ["window_001", "window_002"], "start": 100, "end": 250,
         "search_start": 80, "search_end": 270}]
    return cs.bound_clips(shorts, items, dur, 15.0, 60.0)


def _c(a, b, wid="window_001"):
    return {"start": a, "end": b, "source_window_id": wid, "predicted_score": 70}


def test_bound_drops_reversed_clip_without_repair():
    kept, notes = _bound([_c(100, 90)])
    assert kept == [] and "end <= start" in notes[0]


def test_bound_clamps_to_window_search_span():
    kept, _ = _bound([_c(100, 400)])  # 100-270 clamped, then trimmed to 60 keeping the start
    assert (kept[0]["start"], kept[0]["end"]) == (100, 160)
    kept, notes = _bound([_c(60, 110)])  # starts before search_start
    assert (kept[0]["start"], kept[0]["end"]) == (80, 110) and "clamped" in notes[0]
    kept, _ = _bound([_c(100, 130, "window_002")])  # member id of a merged window
    assert len(kept) == 1


def test_bound_clamps_to_duration_and_drops_short():
    assert _bound([_c(-20, 30)], items=[])[0][0]["start"] == 0.0
    assert _bound([_c(-20, 30)], items=[])[0][0]["end"] == 30
    assert _bound([_c(590, 700)], items=[])[0] == []  # 10 s left on a 600 s video


def test_bound_drops_clip_outside_its_window():
    kept, notes = _bound([_c(400, 450)])
    assert kept == [] and "under 15s" in notes[-1]


def test_bound_trims_long_clip_keeping_start():
    kept, notes = _bound([_c(0, 300)], items=[])
    assert (kept[0]["start"], kept[0]["end"]) == (0, 60) and "trimmed" in notes[0]
