"""Copy for clips cut from non-speech stretches, from text only.

One call for every clip of a job through the caller's stage runner (Gemini or
the local text model), so the prompt stays a few hundred tokens per clip. No
frame and no media ever enters it. A failed call leaves generic copy: the clip
is still returned.
"""
import json

LINES_CHARS = 300   # spoken text sent per clip
TITLE_CHARS = 100


def _chapter_at(chapters, t):
    for c in chapters or []:
        try:
            if float(c["start_time"]) <= t < float(c["end_time"]):
                return str(c.get("title") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return ""


def _lines_before(transcript, t, max_chars=LINES_CHARS):
    segs = [str(g.get("text") or "").strip() for g in (transcript or {}).get("segments", [])
            if float(g.get("end", 0)) <= t and str(g.get("text") or "").strip()]
    return " ".join(segs)[-max_chars:].strip()


def _generic(clip, video_title, chapter):
    title = (chapter or video_title or "Highlight").strip()[:TITLE_CHARS]
    clip.update({"viral_hook_text": title[:60], "video_title_for_youtube_short": title,
                 "video_description_for_tiktok": title, "video_description_for_instagram": title})


def fill_silent_copy(clips, run_stage, video_title, chapters=None, transcript=None, language="en"):
    """Fill the copy fields of ``clips`` in place. ``run_stage(prompt, schema)``
    returns ``(parsed_dict, cost)``; returns the cost list for the caller."""
    import gemini_worker

    if not clips:
        return []
    ctx = []
    for i, c in enumerate(clips):
        chapter = _chapter_at(chapters, float(c["start"]))
        ctx.append({"index": i, "chapter": chapter,
                    "previous_lines": _lines_before(transcript, float(c["start"])),
                    "seconds": round(float(c["end"]) - float(c["start"]))})
    prompt = gemini_worker.SILENT_COPY_PROMPT_TEMPLATE.format(
        video_title=str(video_title or "")[:200], language=language,
        clips_json=json.dumps(ctx, ensure_ascii=False))
    costs = []
    try:
        parsed, cost = run_stage(prompt, gemini_worker.SilentCopyResponse)
        if cost:
            costs.append(cost)
        got = {int(s["index"]): s for s in parsed.get("shorts") or []}
    except Exception as e:
        print(f"   Silent-clip copy failed ({type(e).__name__}); using generic titles.")
        got = {}
    for i, c in enumerate(clips):
        if i in got:
            c.update({k: str(got[i].get(k) or "") for k in (
                "viral_hook_text", "video_title_for_youtube_short",
                "video_description_for_tiktok", "video_description_for_instagram")})
            c["video_title_for_youtube_short"] = c["video_title_for_youtube_short"][:TITLE_CHARS]
        if not c.get("video_title_for_youtube_short"):
            _generic(c, video_title, ctx[i]["chapter"])
    return costs
