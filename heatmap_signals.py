"""YouTube "Most replayed" heatmap as evidence for window scoring.

Mirrors ``twitch_signals``: pure functions over the windows and the heatmap
yt-dlp already returned in the one extraction (never fetched separately).
Heat is coarse (~100 buckets per video, biased to early minutes), so it is
blended into the LLM score with a small weight and never ranks on its own.
"""
import math
import os
from typing import Dict, List, Optional


def normalise(heatmap) -> List[dict]:
    """Keep well-formed ``{start_time, end_time, value}`` buckets, value clamped to 0-1."""
    out = []
    if not isinstance(heatmap, list):
        return out
    for b in heatmap:
        if not isinstance(b, dict):
            continue
        try:
            s, e, v = float(b["start_time"]), float(b["end_time"]), float(b["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in (s, e, v)) or e <= s or s < 0:
            continue
        out.append({"start_time": s, "end_time": e, "value": min(1.0, max(0.0, v))})
    return sorted(out, key=lambda b: b["start_time"])


def window_replay(windows: List[dict], heatmap) -> Dict[str, dict]:
    """``{window_id: {"heat", "peak", "peak_at", "heat_pct"}}`` for windows the heatmap covers.

    ``heat`` is the time-weighted mean over the overlap, ``peak`` the highest
    bucket touching the window, ``heat_pct`` the mid-rank percentile of
    ``heat`` among covered windows (0-1).
    """
    buckets = normalise(heatmap)
    out: Dict[str, dict] = {}
    for w in windows or []:
        covered = total = 0.0
        peak, peak_at = -1.0, 0.0
        for b in buckets:
            ov = min(w["end"], b["end_time"]) - max(w["start"], b["start_time"])
            if ov <= 0:
                continue
            covered += ov
            total += ov * b["value"]
            if b["value"] > peak:
                peak, peak_at = b["value"], (max(w["start"], b["start_time"]) + min(w["end"], b["end_time"])) / 2
        if covered > 0:
            out[w["id"]] = {"heat": total / covered, "peak": peak, "peak_at": peak_at}
    heats = [h["heat"] for h in out.values()]
    for h in out.values():
        less = sum(1 for x in heats if x < h["heat"] - 1e-9)
        equal = sum(1 for x in heats if abs(x - h["heat"]) <= 1e-9)
        h["heat_pct"] = (less + equal / 2) / len(heats)
    return out


def heat_weight() -> float:
    """``REPLAY_HEAT_WEIGHT`` (default 0.3, clamped 0-1); 0 disables the blend."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("REPLAY_HEAT_WEIGHT", "0.3"))))
    except ValueError:
        return 0.3


def blend(score, heat_pct: Optional[float], weight: Optional[float] = None) -> float:
    """``(1-w)*score/100 + w*heat_pct``; a window without heat is neutral (0.5)."""
    w = heat_weight() if weight is None else min(1.0, max(0.0, weight))
    pct = 0.5 if heat_pct is None else heat_pct
    return (1 - w) * float(score or 0) / 100.0 + w * pct


def rank_scored(scored: List[dict], replay: Dict[str, dict], n_windows: int,
                weight: Optional[float] = None) -> List[dict]:
    """``scored`` best first. Blends heat only when it covers >= 50% of the windows."""
    w = heat_weight() if weight is None else weight
    if w <= 0 or not n_windows or len(replay) * 2 < n_windows:
        return sorted(scored, key=lambda s: s.get("score", 0), reverse=True)
    return sorted(scored, reverse=True, key=lambda s: blend(
        s.get("score", 0), (replay.get(s.get("id")) or {}).get("heat_pct"), w))
