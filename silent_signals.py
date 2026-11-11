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
        w["audio_energy"], w["cut_density"] = None, None
        w["peak"] = round(r["peak_at"], 2) if r else None
        lo, hi = int(w["start"] // step), min(len(curve), int(w["end"] // step) + 1)
        seg = curve[lo:hi]
        if seg:
            w["audio_energy"] = round(sum(seg) / len(seg), 3)
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


def merge_rankings(speech_ranked, silent_ranked, n):
    """The global top ``n`` over both groups, best first.

    Each list is already best first; an item's rank within its own group becomes
    a percentile, so neither group starves and the shortlist is exactly ``n``
    whenever there are ``n`` windows (never "up to K per group")."""
    scored = []
    for group, items in ((0, speech_ranked), (1, silent_ranked)):
        for i, it in enumerate(items):
            scored.append((-(1 - (i + 0.5) / len(items)), group, i, it))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:n]]


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
            "video_title_for_youtube_short": None, "viral_hook_text": None,
            "video_description_for_tiktok": None, "video_description_for_instagram": None,
            "why": ""}
