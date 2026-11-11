import inspect

import gemini_worker
import llm_backend
import silent_copy


def _clips(n):
    return [{"start": 100.0 * i + 10, "end": 100.0 * i + 40} for i in range(n)]


TR = {"segments": [{"start": 0, "end": 5, "text": "welcome to the stream " * 30}]}
CHAPTERS = [{"start_time": 0, "end_time": 100, "title": "Final lap"}]


def _stage(captured):
    def run(prompt, schema):
        captured.append((prompt, schema))
        n = prompt.count('"index"')
        return {"shorts": [{"index": i, "viral_hook_text": f"hook {i}", "video_title_for_youtube_short": f"title {i}",
                            "video_description_for_tiktok": "d", "video_description_for_instagram": "d"}
                           for i in range(n)]}, {"input_tokens": 1}
    return run


def test_prompt_carries_title_chapter_and_last_lines_as_text_only():
    seen = []
    silent_copy.fill_silent_copy(_clips(2), _stage(seen), "My Race Day", CHAPTERS, TR)
    assert len(seen) == 1
    prompt, schema = seen[0]
    assert isinstance(prompt, str) and schema is gemini_worker.SilentCopyResponse
    assert "My Race Day" in prompt and "Final lap" in prompt and "welcome to the stream" in prompt
    assert not any(w in prompt.lower() for w in ("image", "frame", "base64", "http"))
    assert "cannot see the\nvideo" in prompt


def test_batch_of_n_clips_yields_n_filled_shorts_in_one_call():
    seen, clips = [], _clips(6)
    silent_copy.fill_silent_copy(clips, _stage(seen), "T", CHAPTERS, TR)
    assert len(seen) == 1
    assert [c["viral_hook_text"] for c in clips] == [f"hook {i}" for i in range(6)]
    assert all(c["video_title_for_youtube_short"] and c["video_description_for_tiktok"] for c in clips)


def test_llm_failure_keeps_the_clip_with_generic_copy():
    clips = _clips(2)

    def boom(prompt, schema):
        raise RuntimeError("tunnel down")

    silent_copy.fill_silent_copy(clips, boom, "My Race Day", CHAPTERS, TR)
    assert clips[0]["video_title_for_youtube_short"] == "Final lap (0:10)"
    assert clips[1]["video_title_for_youtube_short"] == "My Race Day (1:50)"
    assert all(c["viral_hook_text"] and c["start"] < c["end"] for c in clips)


def test_prompt_size_under_local_context_budget():
    seen = []
    long_tr = {"segments": [{"start": 0, "end": 5, "text": "x" * 5000}]}
    silent_copy.fill_silent_copy(_clips(12), _stage(seen), "t" * 500, CHAPTERS, long_tr)
    assert len(seen[0][0]) / 3.0 < 3000   # tokens at a pessimistic 3 chars per token


def test_local_llm_never_gets_media():
    params = inspect.signature(llm_backend.generate_json).parameters
    assert list(params) == ["prompt", "schema", "model"]
    assert params["prompt"].annotation in (str, "str")
    assert not any(w in gemini_worker.SILENT_COPY_PROMPT_TEMPLATE.lower() for w in ("image", "frame", "attach"))


def test_generic_title_is_never_a_bare_highlight():
    clip = {"start": 75.0, "end": 100.0}
    silent_copy._generic(clip, "", "")
    assert clip["video_title_for_youtube_short"] == "Clip at 1:15"


def test_only_lines_spoken_shortly_before_the_clip_are_sent():
    tr = {"segments": [{"start": 0, "end": 5, "text": "ancient history"},
                      {"start": 300, "end": 305, "text": "just said this"}]}
    assert silent_copy._lines_before(tr, 310.0) == "just said this"
    assert silent_copy._lines_before(tr, 600.0) == ""
