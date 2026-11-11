import clip_selection as cs


def _seg(a, b, n=40):
    step = (b - a) / n
    return {"start": a, "end": b, "text": " ".join(f"w{i}" for i in range(n)),
            "words": [{"word": f"w{i}", "start": a + i * step, "end": a + (i + 0.8) * step} for i in range(n)]}


def _talk(a, b):
    return [_seg(t, t + 10) for t in range(int(a), int(b), 10)]


def _tr(*parts):
    return {"language": "en", "segments": [s for p in parts for s in p]}


def test_speech_density_per_stretch():
    tr = _tr(_talk(0, 120), _talk(240, 360))
    dens = cs.speech_density(tr, 360)
    assert len(dens) == 12 and dens[0][:2] == (0.0, 30.0)
    assert all(w > 100 for _, _, w in dens[:4]) and all(w == 0 for _, _, w in dens[4:8])
    runs = cs.silent_stretches(tr, 360)
    assert len(runs) == 1
    assert abs(runs[0][0] - 120) <= 30 and abs(runs[0][1] - 240) <= 30


def test_nonspeech_window_created_only_over_30s_low_density():
    # 20 s gap: no window; 100 s gap: windows of at most 90 s
    short = _tr(_talk(0, 100), _talk(120, 220))
    assert not [w for w in cs.build_timeline_windows(short, 220) if w.get("kind") == "nonspeech"]
    long = _tr(_talk(0, 100), _talk(200, 300))
    ns = [w for w in cs.build_timeline_windows(long, 300) if w.get("kind") == "nonspeech"]
    assert ns and all(w["end"] - w["start"] <= 90 for w in ns)
    assert ns[0]["id"] == "window_ns_001"
    assert {"heat", "peak", "audio_energy", "cut_density"} <= set(ns[0])


def test_text_windows_unchanged_for_pure_speech():
    tr = _tr(_talk(0, 300))
    assert cs.build_timeline_windows(tr, 300) == cs.build_transcript_windows(tr, 300)


def test_nonspeech_window_has_no_text_key_sent_to_llm():
    tr = _tr(_talk(0, 100), _talk(200, 300))
    ws = cs.build_timeline_windows(tr, 300, {"energy": [0.5] * 300})
    ns = [w for w in ws if w.get("kind") == "nonspeech"]
    assert ns and all("text" not in w for w in ns)
    speech = [w for w in ws if w.get("kind") != "nonspeech"]
    assert all("text" in w for w in cs.build_window_payload(speech))


def test_stray_words_in_a_silent_stretch_do_not_make_a_speech_window():
    tr = _tr(_talk(0, 60), [_seg(150, 152, n=2)], _talk(300, 360))
    ws = cs.build_timeline_windows(tr, 360)
    assert not any(w["start"] == 150 for w in ws if w.get("kind") != "nonspeech")
    assert any(w.get("kind") == "nonspeech" for w in ws)


def test_segment_stretched_across_a_pause_is_split_by_word_timestamps():
    # Whisper let one segment run 60 -> 155 s although its words end at 70 s
    words = [{"word": f"w{i}", "start": 60 + i * 0.5, "end": 60.4 + i * 0.5} for i in range(20)]
    stretched = {"start": 60.0, "end": 155.0, "text": " ".join(w["word"] for w in words), "words": words}
    tr = _tr(_talk(0, 60), [stretched], _talk(155, 215))
    assert cs.silent_stretches(tr, 215)[0][0] <= 90
    ws = cs.build_timeline_windows(tr, 215)
    assert max(w["end"] for w in ws if w.get("kind") != "nonspeech" and w["start"] < 100) <= 70.5
    assert [w for w in ws if w.get("kind") == "nonspeech"]
