"""select_clips: speech and non-speech windows first, vision only as the last resort."""
import pytest

SPARSE = {"language": "en", "segments": [{"start": 1.0, "end": 2.0, "text": "uh uh", "words": []}]}
SPEECH = {"language": "en", "segments": [
    {"start": 10.0 * i, "end": 10.0 * i + 9, "text": "word " * 25, "words": []} for i in range(30)]}


def _stub(main, monkeypatch, curve):
    monkeypatch.setattr(main.silent_signals, "audio_energy_curve", lambda *a, **k: curve)
    monkeypatch.setattr(main, "_run_gemini_stage", lambda c, m, p, s: (
        {"shorts": [{"index": i, "viral_hook_text": "H", "video_title_for_youtube_short": "T",
                     "video_description_for_tiktok": "d", "video_description_for_instagram": "d"}
                    for i in range(p.count('"index"'))]}, None))
    visual = []
    monkeypatch.setattr(main, "get_visual_clips", lambda *a, **k: visual.append(a) or {"shorts": [{"start": 0, "end": 20}]})
    return visual


def _peaky():
    c = [0.05] * 600
    for t in (50, 200, 350, 500):
        c[t] = 1.0
    return c


def test_sparse_video_with_energy_peaks_gets_clips_without_gemini(main, monkeypatch):
    visual = _stub(main, monkeypatch, _peaky())
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")   # text-only server, no Gemini key
    data, video, title, vision = main.select_clips(SPARSE, 600.0, "a.m4a", silent={"audio": "a.m4a", "title": "T"})
    assert data and all(c["kind"] == "nonspeech" for c in data["shorts"]) and len(data["shorts"]) >= 2
    assert all(15 <= c["end"] - c["start"] <= 60 for c in data["shorts"])
    assert not visual and not vision and video is None


def test_sparse_without_signal_and_without_gemini_key_raises_the_documented_error(main, monkeypatch):
    _stub(main, monkeypatch, [0.0] * 600)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    with pytest.raises(RuntimeError, match="Clip detection failed.*cannot see the footage"):
        main.select_clips(SPARSE, 600.0, "a.m4a", silent={"audio": "a.m4a"})
    with pytest.raises(RuntimeError, match="Clip detection failed"):
        main.select_clips(None, 600.0, "a.m4a")   # no audio track at all


def test_sparse_without_signal_with_gemini_key_falls_back_to_vision(main, monkeypatch):
    visual = _stub(main, monkeypatch, [0.0] * 600)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(main.genai, "Client", lambda api_key=None: object())
    fetched = []
    data, video, title, vision = main.select_clips(
        SPARSE, 600.0, "a.m4a", silent={"audio": "a.m4a"},
        fetch_full=lambda: fetched.append(1) or ("full.mp4", "Title"))
    assert vision and fetched == [1] and video == "full.mp4" and title == "Title"
    assert visual and visual[0][0] == "full.mp4" and data["shorts"]


def test_speech_that_fails_is_not_sent_to_vision(main, monkeypatch):
    visual = _stub(main, monkeypatch, [0.0] * 600)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(main, "get_viral_clips", lambda *a, **k: None)
    data, _, _, vision = main.select_clips(SPEECH, 300.0, "a.m4a")
    assert data is None and not vision and not visual


def test_no_llm_at_all_says_so(main, monkeypatch):
    _stub(main, monkeypatch, _peaky())
    with pytest.raises(RuntimeError, match="no LLM is configured"):
        main.select_clips(SPARSE, 600.0, "a.m4a", silent={"audio": "a.m4a"})


def test_early_path_hands_title_and_chapters_to_the_silent_copy(main, monkeypatch):
    ctx = main.silent_context("a.m4a", {"title": "Race Day", "chapters": [{"title": "Lap 3"}], "id": "x"})
    assert ctx == {"audio": "a.m4a", "title": "Race Day", "chapters": [{"title": "Lap 3"}]}
    assert main.silent_context("a.m4a") == {"audio": "a.m4a"}
    # and the copy prompt carries it: no empty VIDEO_TITLE on the default (non-range) path
    _stub(main, monkeypatch, _peaky())
    seen = []
    monkeypatch.setattr(main, "_run_gemini_stage", lambda c, m, p, s: seen.append(p) or ({"shorts": []}, None))
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    main.get_viral_clips(SPARSE, 600.0, silent=ctx)
    assert any("VIDEO_TITLE: Race Day" in p for p in seen)
