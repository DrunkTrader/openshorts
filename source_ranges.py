"""Which stretches of a source video to download for the chosen clips.

Pure functions (no network, no heavy imports): the clips are padded so the
precise cut has frames on both sides, overlapping or close pieces are merged
(each ranged download costs an ffmpeg start-up, and a merged piece of a few
minutes is cheap), and a total cap keeps a long selection from turning back
into a full download.
"""
import os

from clip_selection import trim_to_best


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return float(default)


def usable(shorts):
    """The clips with a finite, positive length."""
    out = []
    for c in shorts:
        try:
            if float(c["end"]) > float(c["start"]) >= 0:
                out.append(c)
        except (KeyError, TypeError, ValueError):
            pass
    return out


def pad_ranges(shorts, duration, pad=None):
    """``(start - pad, end + pad)`` per clip, clamped to ``[0, duration]``.
    A clip with zero or negative length raises ValueError."""
    pad = _env_float("RANGE_PAD_SECONDS", 10) if pad is None else float(pad)
    out = []
    for c in shorts:
        s, e = float(c["start"]), float(c["end"])
        if not e > s:
            raise ValueError(f"clip with no length: {s}-{e}")
        out.append((max(0.0, s - pad), min(float(duration), e + pad)))
    return out


def merge_ranges(ranges, gap=None):
    """Sorted ranges, those overlapping or closer than ``gap`` seconds merged."""
    gap = _env_float("RANGE_MERGE_GAP", 8) if gap is None else float(gap)
    merged = []
    for s, e in sorted(ranges):
        if merged and s - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def assign(shorts, merged):
    """``(piece_index, offset)`` per clip: the piece holding its start, and the
    piece's start (the clip's time inside the piece is ``start - offset``)."""
    out = []
    for c in shorts:
        s = float(c["start"])
        for i, (ps, pe) in enumerate(merged):
            if ps <= s < pe:
                out.append((i, ps))
                break
        else:
            raise ValueError(f"no piece holds a clip starting at {s}s")
    return out


def total_seconds(merged):
    return sum(e - s for s, e in merged)


def cap_total(shorts, duration, pad=None, gap=None, max_total=None):
    """Drop the lowest-scored clips until the pieces fit ``max_total`` seconds
    (RANGE_MAX_TOTAL_SECONDS, default 1800). Survivors keep source order."""
    max_total = _env_float("RANGE_MAX_TOTAL_SECONDS", 1800) if max_total is None else float(max_total)
    keep = list(shorts)
    while len(keep) > 1 and total_seconds(
            merge_ranges(pad_ranges(keep, duration, pad), gap)) > max_total:
        keep = trim_to_best(keep, len(keep) - 1)
    if len(keep) < len(shorts):
        print(f"✂️ Range cap: keeping {len(keep)} of {len(shorts)} clips "
              f"(pieces would exceed {max_total:.0f}s).")
    return keep
