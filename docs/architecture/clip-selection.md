# Clip selection

## How many clips a job returns (`clip_selection.py`, `main.get_viral_clips`)

The count is not a setting, it is derived. `get_viral_clips` builds ~90 s scoring
windows over the transcript, scores them in batches, shortlists the best
`shortlist_target` (`min(10, duration//90 + 2)`, floor 3) and asks the detail pass
for `clip_count_targets(len(shortlist))` clips. The floor matters: jobs that
return only 1-3 clips retain far worse than jobs that return 4-9.

Two invariants keep that chain from leaking:

- **The scoring pass ranks, it does not select.** The prompt scores **every**
  window and the shortlist is the global top N. A prompt that says "choose up to
  3 windows from this batch" caps the shortlist at `3 * n_batches` instead of the
  target and starves every source under ~30 min. Batches are near-equal
  (`score_batches`): a trailing batch of one window would return that window
  whatever its score.
- **The clip floor is enforced in code, not just in the prompt.** When the detail
  pass comes back under `min_clips`, the shortlist windows that produced nothing
  get one more call for the difference. An empty answer is accepted: padding is
  worse than a short list.

`target_clips` on `/api/process` (dashboard: advanced options) pins both ends via
`CLIP_TARGET_MIN` / `CLIP_TARGET_MAX` when the user wants an exact number.

## Silent footage: the vision fallback (`main.get_visual_clips`)

`transcribe_video` raising `NoAudioError`, **or** `speech_is_sparse()` returning
true, both set `transcript = None` and route to `get_visual_clips`. Sparse means
under `MIN_SPEECH_WORDS` (8) in total or under `MIN_SPEECH_WORDS_PER_MIN` (5).
Music-only footage and mic-muted recordings transcribe to a handful of stray
words; without the second test the picker scores those words and cuts around
them.

The vision pass uploads the video, Gemini returns the same `{"shorts"}` shape
(`gemini_worker.VisualResponse`) in the same 15-60 s band, so every later stage
is identical. `CLIP_TARGET_MIN`/`MAX` apply directly (there are no scoring
windows). The transcript is stored as `{"language": "none", "segments": []}`, so
the clips have no subtitles, which is correct.

**This is the one stage that sends Gemini the video instead of frames, on
purpose**: frames can say what kind of video it is, not which 40 seconds to cut.
Gemini counts video at ~300 tokens/second, so an hour is ~1.08M tokens, past a 1M
window. A source longer than `VISION_SEGMENT_SECONDS` (2400) is therefore split by
`clip_selection.vision_segments` into near-equal stretches (no one-minute tail),
each re-encoded to a 360p / 1 fps proxy (`_vision_proxy`: re-encoded, not
stream-copied, or every timestamp would be off by up to a GOP), watched on its
own, offset back to source time, then merged with `dedupe_overlapping` +
`trim_to_best` to `CLIP_TARGET_MAX`. One failed stretch is logged and skipped.
`VISION_MAX_SEGMENTS` (8, ~5 h 20) bounds the spend of one job; past it only the
first stretches are watched. A source that fits one stretch is one stretch: it
goes through the same proxy, never as the raw file (a broken capture, such as an
HLS VOD whose audio will not decode, made Gemini answer 400 INVALID_ARGUMENT).
When the proxy's audio cannot be encoded it is retried without audio. Do not
port the frame trick here. Gemini-only: a
text-only `LLM_BASE_URL` server cannot see footage, and without `GEMINI_API_KEY`
the function logs one line and returns None, which fails the job.

The public `/gta-5-clips`, `/gta-6-clips`, `/wow-forever-clips` and `/platforms/twitch` pages state
these thresholds and the stretch sizes; if the behaviour changes, change
`dashboard/seo/pages.js` and `dashboard/seo/clip-tools.js` too.

## Local LLM for the moment picker (`llm_backend.py`)

`LLM_BASE_URL` (+ `LLM_MODEL`, `LLM_API_KEY`) routes the two transcript passes of
`get_viral_clips` to any OpenAI-compatible `/chat/completions` instead of Gemini;
the response is validated with the same pydantic schemas Gemini enforces
server-side, so `main.py` sees one shape. `main.score_batch_size` drops to 3
windows per call there (local contexts are 4-8k; a truncated prompt scores garbage
silently). Self-host `/api/process` then accepts a request without
`X-Gemini-Key` and `/api/config.localLlm` tells the dashboard not to demand one.
Frame-based stages (`layout_picker`, `screencast_layout`, `get_visual_clips`)
stay on Gemini. Never wired in cloud mode: `BILLING_ENABLED` ignores it.

## Range jobs: pieces instead of a source (`source_ranges.py`, `range_ingest.py`)

Self-host URL jobs pick moments from the audio transcript alone, then download
`[start - pad, end + pad]` pieces (`RANGE_PAD_SECONDS`, default 10; ranges closer
than `RANGE_MERGE_GAP`, default 8 s, merge). `RANGE_MAX_TOTAL_SECONDS` (default
1800) caps the total: the lowest-scored clips go first, but never below the
clip-count floor (`clip_count_targets`); past it the log says the floor won.
A clip starting at or after the audio's end is dropped, one ending past it is
clamped. A clip whose piece failed twice is dropped (logged), and the job fails
only when none is left. Sparse speech (the vision fallback needs frames) takes
the legacy full download. Clip dicts keep absolute source time; each cut is
`cut_clip(piece, start - piece_start, end - piece_start)`. Metadata carries
`source_pieces` (file, start, end), `source_url` and `source_duration`;
recut, re-frame, scenes and the editor monitor resolve the piece that holds the
segments (`app._locate_source_for_range`) and answer 409 when none does. The
layout picker and burned-subtitle probe do not run on range jobs yet.

## Replay heat and straddling windows

- YouTube's "Most replayed" heatmap arrives with the one yt-dlp extraction (range jobs only, YouTube extractor only; never fetched on its own). `heatmap_signals` time-weights it per window; the windows carry `replay_heat`/`replay_peak` in the scoring prompt, and the shortlist order blends `score/100 + w*(heat_pct - 0.5)` into windows that have heat (windows without keep their pure score) only when at least half the windows have heat. `REPLAY_HEAT_WEIGHT` (default 0.3, 0 disables). Heat is evidence, never the only rank.
- Before the detail pass, consecutive shortlisted windows are merged (`clip_selection.merge_adjacent`, text rebuilt from segments so the 30 s overlap counts once, first id kept as `source_window_id`, run capped at twice the window size) and each gets `context_before`/`context_after` (`DETAIL_NEIGHBOUR_CHARS`, default 600 per side) with the seconds they open (`search_start`/`search_end`). A clip may use that context but must start and end inside those bounds. A local LLM gets the detail list in batches of at most `DETAIL_BATCH_CHARS` of text (default 9000, 0 = one call). `build_transcript_windows` is unchanged.
- Merging and neighbour context apply to every backend, Gemini included; only `detail_batches` is local-LLM only. Each batch is asked for its share of the clip band (the global floor stays in code). The char budget is calibrated on Latin text (~3.5 chars/token); CJK runs about twice the tokens, so lower `DETAIL_BATCH_CHARS` there.
- Clips leave the detail pass through `clip_selection.bound_clips` (drop end <= start, clamp to [0, duration] and the source window's `search_start`/`search_end`, drop under the minimum, trim over the maximum keeping the start), then the word snap and `dedupe_overlapping`. The `min_clips` floor is judged after dedupe and reuses the same retry over windows that kept no clip; a merged window counts as used when the model echoes any member id.
