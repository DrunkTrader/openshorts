"""
Pure helpers for the Gemini clip-selection pipeline.

Standard-library only so both main.py and gemini_worker.py can import it and
the logic stays unit-testable without the heavy video dependencies.
"""
import math

# USD per 1M tokens (input, output incl. thinking), from ai.google.dev pricing.
MODEL_PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),  # deprecated (shut down 2026-06-01)
}


def lookup_model_prices(model_name):
    """Longest-prefix match against MODEL_PRICES; None if unknown."""
    name = str(model_name or "").lower()
    best_key = None
    for key in MODEL_PRICES:
        if name.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return MODEL_PRICES[best_key] if best_key else None


def clip_count_targets(n_windows):
    """How many clips to ask the detail pass for, given the shortlist size.

    Measured on prod 3-ago-2026: 408 of 429 jobs (95%) delivered 3 clips or
    fewer, the mode being ONE, while the prompt was free to return one per
    shortlisted window. Users who received 1-3 clips came back a second day
    0.4% of the time; those who received 4-9 came back 16.1% — so the clip
    count, not the clip quality, is what the retention curve hangs on.

    The old prompt biased hard the other way ("prefer one great clip per
    candidate window") and handed the model two unbounded licences to drop
    clips (the 2-second rule and STANDS ALONE both end in "or skip it"), with
    no floor to stop it collapsing to a single clip. This puts a floor and a
    realistic ceiling on it instead.

    ``CLIP_TARGET_MIN`` / ``CLIP_TARGET_MAX`` override both for A/B runs
    without a deploy (the reframe-testing harness drives them).
    """
    import os

    n = max(1, int(n_windows or 1))
    # Floor grows with the material: 3 windows -> 3, 5 -> 4, 10+ -> 6.
    low = max(2, min(6, n // 2 + 2))
    # Ceiling allows a rich window to yield more than one without inviting padding.
    high = min(12, max(4, n * 2))
    low = min(low, high)

    def _override(name, current):
        raw = os.environ.get(name)
        if not raw:
            return current
        try:
            return max(1, int(raw))
        except ValueError:
            return current

    low = _override("CLIP_TARGET_MIN", low)
    high = _override("CLIP_TARGET_MAX", high)
    return low, max(low, high)


def shortlist_target(video_duration):
    """How many scored windows the detail pass should get.

    Scales with duration so a long video surfaces more candidates without
    exploding the detail call, floored at 3 so a short one still gets a
    choice. ``score_batches`` exists to make this number reachable.
    """
    try:
        seconds = float(video_duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    return max(3, min(10, int(seconds // 90) + 2))


def score_batches(windows, batch_size):
    """Split ``windows`` into near-equal scoring batches.

    Two things were wrong with the old ``range(0, n, batch_size)`` walk plus a
    prompt that said "choose up to 3 windows from this batch".

    The cap made the shortlist unreachable. Its real ceiling was
    ``3 * n_batches``, not ``shortlist_target``: measured 22-sep-2026 on a
    9:10 source, 9 windows batched 8 + 1 returned 3 + 1 = 4 windows against a
    target of 8. ``clip_count_targets`` then derives the clip floor from that
    halved shortlist, so the detail pass was asked for 4-8 clips instead of
    6-12 and the job delivered 4 (3 in prod on the same video). Every source
    under ~30 minutes was starved the same way, which is the mechanism behind
    "95% of jobs deliver 3 clips or fewer" — the clip count the retention
    curve hangs on (see clip_count_targets). The prompt now scores every
    window it is given and the shortlist is the global top ``target``, so the
    selection happens where the scores can actually be compared.

    Per-batch selection also threw away the ranking it was computing: a batch
    holding five great moments could only contribute three, while a batch of
    filler still contributed its own three.

    The remainder was its own bug: a trailing batch of one window came back
    with that window whatever its score, because a cap of 3 cannot filter a
    batch of 1 — the tail of the video entered the shortlist by arithmetic
    rather than merit (``window_009`` did exactly that on the measured run).
    Near-equal batches keep that from happening and also keep each score
    comparable, since a window judged alone is judged against nothing.
    """
    windows = list(windows or [])
    n = len(windows)
    if not n:
        return []
    size = max(1, int(batch_size or 1))
    n_batches = max(1, -(-n // size))       # ceil
    base, extra = divmod(n, n_batches)

    out = []
    start = 0
    for index in range(n_batches):
        take = base + (1 if index < extra else 0)
        batch = windows[start:start + take]
        start += take
        if batch:
            out.append(batch)
    return out


def trim_to_best(shorts, max_clips):
    """Cut an over-long detail-pass result down to ``max_clips`` BY SCORE.

    The detail pass hands its clips back in transcript order, batch after
    batch, so slicing the list keeps the EARLIEST clips rather than the best
    ones. On a 9-minute walkthrough that quietly threw away everything past
    minute three: the model proposed clips across the whole video, and the
    ones covering the demo, the MCP walkthrough and the close were the tail
    that got dropped. Worse, the failure scales the wrong way — the more
    generous the model is, the more of the video disappears.

    That sabotages the windowing: get_viral_clips builds scoring windows
    precisely because "a single call over the whole transcript clusters picks
    near the start", and a positional slice puts the clustering right back.

    Ranking is by ``predicted_score`` (the detail prompt already asks for it,
    and nothing else was reading it here). Ties keep transcript order, and the
    survivors come back in transcript order too, so clip numbering still runs
    front to back the way every caller downstream expects.
    """
    max_clips = max(1, int(max_clips or 1))
    if len(shorts) <= max_clips:
        return list(shorts)

    def score(item):
        try:
            return float(item[1].get("predicted_score") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    indexed = list(enumerate(shorts))
    best = sorted(indexed, key=score, reverse=True)[:max_clips]
    return [item for _, item in sorted(best, key=lambda pair: pair[0])]


def vision_segments(video_duration, segment_seconds, max_segments):
    """Split a silent source into near-equal ``(start, end)`` stretches for
    the vision pass, each at most ``segment_seconds`` long.

    The vision pass sends Gemini the footage itself at ~300 tokens/second, so
    an hour (~1.08M tokens) no longer fits a 1M-token window and a silent
    multi-hour capture failed at the model. Near-equal stretches, not fixed
    ones: a 41-minute source split at 40 minutes would leave a one-minute
    tail that the prompt has to fill with 3+ clips. A source that fits in one
    stretch returns one stretch, so short videos take the single-upload path
    unchanged. Past ``max_segments`` the source is truncated (the caller logs
    it) rather than spending without bound on one job.
    """
    duration = max(0.0, float(video_duration or 0))
    seg = max(60.0, float(segment_seconds))
    if duration <= seg:
        return [(0.0, round(duration, 3))]
    n = int(math.ceil(duration / seg))
    n_kept = max(1, min(n, int(max_segments)))
    step = duration / n
    return [(round(i * step, 3), round(min(duration, (i + 1) * step), 3))
            for i in range(n_kept)]


def clip_duration_bounds():
    """The clip length band (seconds) the selection prompts and word-snapping
    enforce. ``CLIP_MIN_SECONDS`` / ``CLIP_MAX_SECONDS`` override the classic
    15-60 — set per job by /api/process when the user asks for a specific
    length, or by hand for A/B runs. Values are clamped to platform-sane
    limits and re-ordered so bad input degrades instead of breaking the job.
    """
    import os

    def _read(name, default):
        try:
            return float(os.environ.get(name, ""))
        except ValueError:
            return default

    lo = _read("CLIP_MIN_SECONDS", 15.0)
    hi = _read("CLIP_MAX_SECONDS", 60.0)
    lo = min(max(lo, 5.0), 175.0)
    hi = min(max(hi, 10.0), 180.0)
    if hi < lo + 5.0:  # keep a real band: degenerate ranges starve the model
        hi = min(180.0, lo + 5.0)
    return round(lo, 3), round(hi, 3)


def compact_words(words, precision=2):
    """Round word timestamps for prompts — full float precision wastes tokens."""
    return [
        {
            "w": w.get("w", ""),
            "s": round(float(w.get("s", 0)), precision),
            "e": round(float(w.get("e", 0)), precision),
        }
        for w in words
    ]


def build_transcript_windows(transcript_result, video_duration,
                             window_seconds=90, overlap_seconds=30):
    """
    Build scoring windows aligned to Whisper segment boundaries, so a sentence
    (and usually a viral moment) is never cut in half mid-window. Windows grow
    segment by segment to roughly window_seconds (up to 1.25x for the closing
    segment) and the next window starts ~overlap_seconds before the previous
    end, also snapped to a segment start.
    """
    segments = []
    for segment in transcript_result.get("segments", []):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        segments.append((float(segment.get("start", 0)), float(segment.get("end", 0)), text))

    windows = []
    window_index = 1
    i = 0
    n = len(segments)
    while i < n:
        w_start = segments[i][0]
        j = i
        # Extend while the NEXT segment still fits within a tolerant cap, so the
        # window closes on a segment boundary near window_seconds.
        while j + 1 < n and segments[j + 1][1] - w_start <= window_seconds * 1.25:
            j += 1
            if segments[j][1] - w_start >= window_seconds:
                break
        w_end = segments[j][1]
        windows.append({
            "id": f"window_{window_index:03d}",
            "start": round(w_start, 3),
            "end": round(w_end, 3),
            "text": " ".join(seg[2] for seg in segments[i:j + 1]),
        })
        window_index += 1

        if j >= n - 1:
            break
        # Next window starts at the first segment beginning after (end - overlap),
        # but always makes progress.
        target = w_end - overlap_seconds
        k = i + 1
        while k <= j and segments[k][0] < target:
            k += 1
        i = max(k, i + 1)

    if not windows:
        windows.append({
            "id": "window_001",
            "start": 0.0,
            "end": round(float(video_duration), 3),
            "text": str(transcript_result.get("text", "") or ""),
        })
    return windows


def speech_density(transcript_result, video_duration, stretch=30.0):
    """``[(start, end, words_per_min)]`` over the whole timeline in ``stretch``
    steps (the last one may be shorter). A segment's words spread over its
    duration, so a transcript without word timestamps still counts."""
    duration = float(video_duration or 0)
    segs = [(float(g.get("start", 0)), float(g.get("end", 0)), len(str(g.get("text") or "").split()))
            for g in (transcript_result or {}).get("segments", [])]
    out, a = [], 0.0
    while a < duration - 1e-6:
        b = min(a + stretch, duration)
        words = 0.0
        for s, e, n in segs:
            if n and e > s:
                words += n * max(0.0, min(e, b) - max(s, a)) / (e - s)
            elif n and a <= s < b:
                words += n
        out.append((round(a, 3), round(b, 3), words / ((b - a) / 60.0)))
        a = b
    return out


def silent_stretches(transcript_result, video_duration, min_len=30.0, max_wpm=5.0):
    """``[(start, end)]`` runs of consecutive stretches at or under ``max_wpm``
    that last at least ``min_len`` seconds."""
    runs, cur = [], None
    for a, b, wpm in speech_density(transcript_result, video_duration):
        if wpm <= max_wpm:
            cur = (cur[0], b) if cur else (a, b)
        elif cur:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    return [r for r in runs if r[1] - r[0] >= min_len - 1e-6]


def build_timeline_windows(transcript_result, video_duration, signals=None,
                           window_seconds=90, overlap_seconds=30):
    """Windows over the whole timeline: the speech windows of
    ``build_transcript_windows`` (unchanged) plus ``kind="nonspeech"`` windows
    (``window_ns_001``, at most ``window_seconds``, no ``text``) over silent
    stretches of 30 s or more, with their signals attached
    (``silent_signals.window_signals``). A speech window that lies inside a
    silent stretch (stray words, not speech) is dropped. Silent windows are not
    ranked here; ``silent_signals.score_nonspeech`` does that."""
    import silent_signals

    has_text = any(str(g.get("text") or "").strip() for g in (transcript_result or {}).get("segments", []))
    speech = build_transcript_windows(transcript_result, video_duration,
                                      window_seconds, overlap_seconds) if has_text else []
    stretches = silent_stretches(transcript_result, video_duration)
    if not stretches:
        return speech
    speech = [w for w in speech
              if not any(a - 1e-3 <= w["start"] and w["end"] <= b + 1e-3 for a, b in stretches)]
    silent = []
    for a, b in stretches:
        n = max(1, math.ceil((b - a) / window_seconds - 1e-9))
        step = (b - a) / n
        for i in range(n):
            silent.append({"id": f"window_ns_{len(silent) + 1:03d}", "kind": "nonspeech",
                           "start": round(a + i * step, 3), "end": round(a + (i + 1) * step, 3)})
    silent_signals.window_signals(silent, signals)
    return speech + silent


def build_window_payload(windows, heat=None, replay=None):
    """The window dicts sent to the LLM. ``heat`` (twitch_signals.window_heat)
    and ``replay`` (heatmap_signals.window_replay) add their fields only to the
    windows they cover; extra keys on a window (merged detail windows) pass through (a merged window lists its member ids)."""
    heat, replay = heat or {}, replay or {}
    out = []
    for w in windows:
        item = {"id": w["id"], "start": w["start"], "end": w["end"], "text": w["text"]}
        item.update({k: w[k] for k in ("ids", "context_before", "context_after", "search_start", "search_end")
                     if k in w})
        h = heat.get(w["id"])
        if h:
            item["viewers_clipped"] = h["clips"]
            item["viewer_clip_views"] = h["views"]
            item["viewer_moments"] = h["spans"][:5]
        r = replay.get(w["id"])
        if r:
            item["replay_heat"] = round(r["heat"], 2)
            item["replay_peak"] = round(r["peak"], 2)
        out.append(item)
    return out


def _timed_segments(transcript_result):
    return [(float(g.get("start", 0)), float(g.get("end", 0)), str(g.get("text") or "").strip())
            for g in (transcript_result or {}).get("segments", []) if str(g.get("text") or "").strip()]


def _text_between(segs, start, end):
    """Segments lying inside [start, end]: cut by time, never by string match."""
    return " ".join(t for s, e, t in segs if s >= start - 1e-3 and e <= end + 1e-3)


def merge_adjacent(shortlist, windows, transcript_result, max_span):
    """Merge runs of consecutive shortlisted windows into one detail window.

    A moment that straddles a window seam is whole in neither window. The merged
    window keeps the first id (``source_window_id``), spans the run and rebuilds
    its text from the segments inside it, so the 30 s overlap is counted once.
    A run is cut when it would exceed ``max_span`` seconds. Order follows the
    best-ranked member of each group, like the shortlist it replaces.
    """
    segs = _timed_segments(transcript_result)
    if not segs:
        return [dict(w) for w in shortlist]
    pos = {w["id"]: i for i, w in enumerate(windows)}
    rank = {w["id"]: r for r, w in enumerate(shortlist)}
    ordered = sorted((w for w in shortlist if w["id"] in pos), key=lambda w: pos[w["id"]])
    groups = []
    for w in ordered:
        last = groups[-1] if groups else None
        if last and pos[w["id"]] == pos[last[-1]["id"]] + 1 and w["end"] - last[0]["start"] <= max_span:
            last.append(w)
        else:
            groups.append([w])
    groups.sort(key=lambda g: min(rank[w["id"]] for w in g))
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(dict(g[0]))
            continue
        start, end = g[0]["start"], g[-1]["end"]
        out.append({"id": g[0]["id"], "ids": [w["id"] for w in g], "start": start, "end": end,
                    "text": _text_between(segs, start, end)})
    return out


def neighbour_context(items, transcript_result, max_chars=600):
    """``items`` plus the spoken lines just outside each (at most ``max_chars``
    per side, whole segments), as ``context_before`` / ``context_after`` and the
    seconds they open up: ``search_start`` / ``search_end``."""
    segs = _timed_segments(transcript_result)
    out = []
    for w in items:
        before, used, lo = [], 0, w["start"]
        for s, e, t in reversed([x for x in segs if x[1] <= w["start"] + 1e-3]):
            if used + len(t) > max_chars:
                break
            before.insert(0, t)
            used, lo = used + len(t) + 1, s
        after, used, hi = [], 0, w["end"]
        for s, e, t in [x for x in segs if x[0] >= w["end"] - 1e-3]:
            if used + len(t) > max_chars:
                break
            after.append(t)
            used, hi = used + len(t) + 1, e
        out.append({**w, "context_before": " ".join(before), "context_after": " ".join(after),
                    "search_start": round(lo, 3), "search_end": round(hi, 3)})
    return out


def detail_batches(items, max_chars):
    """Greedy batches of detail items under ``max_chars`` of JSON text (a lone
    oversized item still goes alone). For small-context local models."""
    batches, cur, size = [], [], 0
    for w in items:
        n = sum(len(str(w.get(k) or "")) for k in ("text", "context_before", "context_after"))
        if cur and size + n > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += n
    if cur:
        batches.append(cur)
    return batches


def bound_clips(shorts, items, video_duration, min_secs, max_secs):
    """Hold the model's clips to what the prompt only asked of it.

    Returns ``(kept, notes)``; ``notes`` are short reasons, never transcript
    text. A clip with end <= start is dropped (never repaired); the rest are
    clamped to [0, duration] and to the [search_start, search_end] of their
    source window (a merged window answers to any member id), dropped when what
    is left is under ``min_secs`` and trimmed to ``max_secs`` keeping the start.
    """
    owner = {}
    for w in items:
        for i in w.get("ids") or [w["id"]]:
            owner[i] = w
    kept, notes = [], []
    for c in shorts:
        try:
            a, b = float(c.get("start", 0)), float(c.get("end", 0))
        except (TypeError, ValueError):
            notes.append("dropped a clip with a non-numeric start/end")
            continue
        tag = c.get("source_window_id") or "?"
        if not b > a:
            notes.append(f"dropped {tag}: end <= start")
            continue
        lo, hi = 0.0, float(video_duration)
        w = owner.get(str(c.get("source_window_id") or ""))
        if w is not None:
            lo, hi = max(lo, w.get("search_start", w["start"])), min(hi, w.get("search_end", w["end"]))
        na, nb = min(max(a, lo), hi), min(max(b, lo), hi)
        if (na, nb) != (a, b):
            notes.append(f"clamped {tag}: {a:g}-{b:g} -> {na:g}-{nb:g}")
        if nb - na < min_secs:
            notes.append(f"dropped {tag}: under {min_secs:g}s after clamping")
            continue
        if nb - na > max_secs:
            nb = na + max_secs
            notes.append(f"trimmed {tag} to {max_secs:g}s")
        kept.append({**c, "start": round(na, 3), "end": round(nb, 3)})
    return kept, notes


def dedupe_overlapping(shorts, ratio=0.25):
    """Drop clips that overlap an already-kept clip by ``ratio`` of the
    shorter one, keeping the higher ``predicted_score`` (earlier on a tie).

    0.25, not 0.5: two clips sharing 25 s of 59 s (42%) both survived the old
    bar and the same punchline came out twice in one batch.

    The detail prompt's DIVERSITY rule is the only thing that stopped two
    clips from sharing the same seconds, and a rule is not a guarantee: two
    picks from one window can cover the same moment with different edges,
    and the user then downloads the same short twice. Survivors come back in
    transcript order, like ``trim_to_best``.
    """
    def _score(c):
        try:
            return float(c.get("predicted_score") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _span(c):
        try:
            return float(c.get("start", 0)), float(c.get("end", 0))
        except (TypeError, ValueError, AttributeError):
            return 0.0, 0.0

    indexed = list(enumerate(shorts))
    kept = []
    for idx, clip in sorted(indexed, key=lambda p: (-_score(p[1]), p[0])):
        s, e = _span(clip)
        clash = False
        for _, other in kept:
            os_, oe = _span(other)
            overlap = min(e, oe) - max(s, os_)
            shorter = max(1e-6, min(e - s, oe - os_))
            if overlap > 0 and overlap / shorter >= ratio:
                clash = True
                break
        if not clash:
            kept.append((idx, clip))
    return [c for _, c in sorted(kept, key=lambda p: p[0])]


def snap_clip_to_words(start, end, words, video_duration,
                       min_duration=15.0, max_duration=60.0,
                       search_window=1.5, max_lead=0.35, max_tail=0.45):
    """
    Snap Gemini-proposed clip boundaries onto real word boundaries plus a bit
    of the surrounding silence. LLMs are bad at millisecond arithmetic; the
    word-level timestamps are ground truth, so cuts land in pauses instead of
    mid-word.

    words: [{'w','s','e'}, ...] for the whole video, sorted by start.
    Returns (start, end); falls back to the input if no words are nearby or
    snapping cannot satisfy the duration bounds.
    """
    original = (round(float(start), 3), round(float(end), 3))
    if not words:
        return original

    starts = [float(w.get("s", 0)) for w in words]
    ends = [float(w.get("e", 0)) for w in words]

    # START: snap to the nearest word start, then lead into the silence before it.
    new_start = float(start)
    candidates = [s for s in starts if abs(s - new_start) <= search_window]
    if candidates:
        word_start = min(candidates, key=lambda s: abs(s - new_start))
        prev_ends = [e for e in ends if e <= word_start]
        if prev_ends:
            gap = max(0.0, word_start - max(prev_ends))
            lead = min(max_lead, gap / 2)
        else:
            lead = max_lead
        new_start = max(0.0, word_start - lead)

    # END: snap to the nearest word end, then trail into the silence after it.
    new_end = float(end)
    candidates = [e for e in ends if abs(e - new_end) <= search_window]
    if candidates:
        word_end = min(candidates, key=lambda e: abs(e - new_end))
        next_starts = [s for s in starts if s >= word_end]
        if next_starts:
            gap = max(0.0, min(next_starts) - word_end)
            tail = min(max_tail, gap / 2)
        else:
            tail = max_tail
        new_end = min(float(video_duration), word_end + tail)

    # Repair duration bounds while staying on word boundaries.
    if new_end - new_start < min_duration:
        target = new_start + min_duration
        later = sorted(e for e in ends if e >= target)
        if later and later[0] - new_start <= max_duration:
            new_end = min(float(video_duration), later[0] + 0.2)
        else:
            return original
    if new_end - new_start > max_duration:
        target = new_start + max_duration
        earlier = [e for e in ends if new_start < e <= target]
        new_end = (max(earlier) + 0.2) if earlier else target
        new_end = min(new_end, new_start + max_duration, float(video_duration))

    if new_end <= new_start or new_end - new_start < min_duration:
        return original
    return (round(new_start, 3), round(new_end, 3))
