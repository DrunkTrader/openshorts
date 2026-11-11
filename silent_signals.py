"""Scoring and cutting for stretches with no speech, in code, from signals.

Footage without speech (music, gameplay, a muted mic) has nothing for a text
model to read, so its windows are ranked here from the replay heatmap and the
audio energy, and cut around the loudest moment. The model only writes the copy
(``gemini_worker.SILENT_COPY_PROMPT_TEMPLATE``) and never sees a frame.

Pure functions over plain lists. Selection happens before any video exists
(audio-first), so cut density from scene detection is not a signal here; the
windows carry the field as ``None``.
"""
import os

ENERGY_STEP = 1.0   # seconds per audio-energy sample


def _weight(name, default):
    try:
        return max(0.0, float(os.environ.get(name, "")))
    except ValueError:
        return default


def audio_energy_curve(audio_path, duration, step=ENERGY_STEP):
    """RMS per ``step`` seconds, 0-1 against the file's own loudest sample
    (``active_speaker.audio_envelope``; [] when there is no readable audio)."""
    import active_speaker
    return active_speaker.audio_envelope(audio_path, 0.0, float(duration), window_s=step)


def window_signals(windows, signals):
    """Add ``heat``, ``peak`` (seconds of the strongest moment), ``audio_energy``
    and ``cut_density`` to non-speech ``windows`` in place; each is None when
    its signal does not cover the window. ``signals``: ``heatmap``, ``energy``
    (list), ``energy_step``."""
    import heatmap_signals
    signals = signals or {}
    replay = heatmap_signals.window_replay(windows, signals.get("heatmap")) if signals.get("heatmap") else {}
    curve, step = signals.get("energy") or [], float(signals.get("energy_step") or ENERGY_STEP)
    for w in windows:
        r = replay.get(w["id"])
        w["heat"] = round(r["heat"], 3) if r else None
        w["audio_energy"], w["cut_density"], w["peak_energy"] = None, None, None
        w["peak"] = round(r["peak_at"], 2) if r else None
        lo, hi = int(w["start"] // step), min(len(curve), int(w["end"] // step) + 1)
        seg = curve[lo:hi]
        if seg:
            w["audio_energy"] = round(sum(seg) / len(seg), 3)
            w["peak_energy"] = round(max(seg), 3)
            if max(seg) > 0:
                w["peak"] = round((lo + seg.index(max(seg)) + 0.5) * step, 2)
    return windows


def has_signal(w):
    """A window with no heat and no audible energy is skipped, never cut at random."""
    return w.get("heat") is not None or bool(w.get("audio_energy"))


def _percentiles(values):
    """Mid-rank percentile (0-1) of each value within its own list."""
    return [(sum(1 for y in values if y < x) + sum(1 for y in values if y == x) / 2) / len(values)
            for x in values]


def score_nonspeech(windows):
    """``windows`` with a signal, each given ``score_pct`` and best first.

    Percentile within the group, then ``0.6 * heat_pct + 0.4 * energy_pct``
    (``SILENT_HEAT_WEIGHT`` / ``SILENT_ENERGY_WEIGHT``); a window missing one
    signal uses the other alone. Windows without any signal are dropped.
    """
    ws = [w for w in windows if has_signal(w)]
    if not ws:
        return []
    wh, we = _weight("SILENT_HEAT_WEIGHT", 0.6), _weight("SILENT_ENERGY_WEIGHT", 0.4)
    heat_ws = [w for w in ws if w.get("heat") is not None]
    heat_pct = dict(zip((w["id"] for w in heat_ws), _percentiles([w["heat"] for w in heat_ws]))) if heat_ws else {}
    en_ws = [w for w in ws if w.get("audio_energy") is not None]
    en_pct = dict(zip((w["id"] for w in en_ws), _percentiles([w["audio_energy"] for w in en_ws]))) if en_ws else {}
    for w in ws:
        parts = [(wt, p[w["id"]]) for wt, p in ((wh, heat_pct), (we, en_pct)) if w["id"] in p]
        total = sum(wt for wt, _ in parts)
        w["score_pct"] = round(sum(wt * v for wt, v in parts) / total, 4) if total > 0 else 0.0
    return sorted(ws, key=lambda w: w["score_pct"], reverse=True)


def speech_min_score():
    """LLM score a speech window needs to count as usable (``SILENT_SPEECH_MIN_SCORE``,
    default 50: the scoring prompt puts filler under 30 and the clippable at 70+)."""
    return _weight("SILENT_SPEECH_MIN_SCORE", 50.0)


def energy_bar(curve, percentile=None):
    """The value a window's loudest second must reach to count as a real moment:
    the ``SILENT_BAR_PERCENTILE`` (default 90) of the job's WHOLE energy curve,
    speech included, not of the silent windows alone. None without a curve."""
    if not curve:
        return None
    pct = percentile if percentile is not None else _weight("SILENT_BAR_PERCENTILE", 90.0)
    ordered = sorted(curve)
    return ordered[min(len(ordered) - 1, int(len(ordered) * min(pct, 100.0) / 100.0))]


def passes_bar(w, bar, heat_bar=0.5):
    """A non-speech window with its own evidence: a loudest second at or above
    ``bar`` (``energy_bar``), or a replay heat of at least ``heat_bar`` (0-1)."""
    pe = w.get("peak_energy")
    return (pe is not None and bar is not None and pe >= bar) or \
        (w.get("heat") is not None and w["heat"] >= heat_bar)


def merge_rankings(speech_ranked, silent_ranked, n, speech_ok=None, silent_ok=None):
    """The shortlist: ``n`` windows out of both groups, best first; every window
    of both groups gets ``rank_score`` (0-1) from the merged order.

    One global ranking over every window, never "up to K per batch": each list
    is best first, an item's rank in its own group is a percentile
    (``1 - (i + .5) / len``), and the top ``n`` by that percentile is taken. A
    percentile alone starves a small group (a lone strong silent stretch always
    scores 0.5), so a group that has at least one window passing its own bar
    (``speech_ok`` / ``silent_ok``, a score or signal not relative to the group)
    is guaranteed ``max(1, round(n * its share of all windows))`` slots, taken
    from its passing windows; the rest of the slots go by global percentile.
    A group with nothing passing its bar gets no reserved slot."""
    groups = [(0, list(speech_ranked), speech_ok), (1, list(silent_ranked), silent_ok)]
    total = sum(len(g[1]) for g in groups)
    entries = []   # (neg percentile, group, rank, window)
    passing, take = {}, {}
    for gi, items, ok in groups:
        for i, w in enumerate(items):
            entries.append((-(1 - (i + 0.5) / len(items)), gi, i, w))
        passing[gi] = [i for i, w in enumerate(items) if ok is not None and ok(w)]
        if passing[gi]:
            take[gi] = min(len(passing[gi]), max(1, round(n * len(items) / total)))
    while sum(take.values()) > n:      # the reserved slots never exceed the target
        take[max(take, key=take.get)] -= 1
    chosen = {(gi, i) for gi, k in take.items() for i in passing[gi][:k]}
    entries.sort(key=lambda t: t[:3])
    picked = [e for e in entries if (e[1], e[2]) in chosen][:n]
    picked_ids = {(e[1], e[2]) for e in picked}
    for e in entries:
        if len(picked) >= n:
            break
        if (e[1], e[2]) not in picked_ids:
            picked.append(e)
            picked_ids.add((e[1], e[2]))
    picked.sort(key=lambda t: t[:3])
    rest = [e for e in entries if (e[1], e[2]) not in picked_ids]
    for pos, e in enumerate(picked + rest):
        if isinstance(e[3], dict):
            e[3]["rank_score"] = round(1 - pos / max(1, total), 4)
    return [e[3] for e in picked]


def cut_from_peak(window, curve, min_s, max_s, step=ENERGY_STEP):
    """A clip around the window's peak, or None when the window cannot hold ``min_s``.

    Starts 4 s before the peak, ends 1.5 s after the energy first falls under
    35% of the peak value (no curve: a 30 s stretch), clamped to ``min_s``-``max_s``
    and to the window. The peak always lies inside. Copy fields are None for
    ``gemini_worker.fill_silent_copy``."""
    ws, we = float(window["start"]), float(window["end"])
    peak = window.get("peak")
    if peak is None or we - ws < min_s:
        return None
    peak = min(max(float(peak), ws), we)
    start = max(ws, peak - 4.0)
    i = int(peak // step)
    if curve and i < len(curve) and curve[i] > 0:
        end = we
        for j in range(i + 1, min(len(curve), int(we // step) + 1)):
            if curve[j] < 0.35 * curve[i]:
                end = j * step + 1.5
                break
    else:
        end = start + 30.0
    end = min(max(end, start + min_s), start + max_s, we)
    start = max(ws, min(start, end - min_s))   # at the window's right edge, move the start back
    if end - start > max_s:
        end = start + max_s
    return {"start": round(start, 3), "end": round(end, 3),
            "predicted_score": int(round(100 * float(window.get("score_pct") or 0))),
            "source_window_id": window["id"], "kind": "nonspeech",
            "rank_score": window.get("rank_score"),
            "video_title_for_youtube_short": None, "viral_hook_text": None,
            "video_description_for_tiktok": None, "video_description_for_instagram": None,
            "why": "Loudest or most replayed moment of a stretch without speech"}
