"""Which stretches of a source video to download for the chosen clips.

Pure functions (no network, no heavy imports): the clips are padded so the
precise cut has frames on both sides, overlapping or close pieces are merged
(each ranged download costs an ffmpeg start-up, and a merged piece of a few
minutes is cheap), and a total cap keeps a long selection from turning back
into a full download.
"""
import json
import os

from clip_selection import trim_to_best


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return float(default)


def usable(shorts, duration=None):
    """The clips with a finite, positive length that start inside the source
    (a clip starting at or past ``duration`` is dropped; one ending past it is
    clamped to it)."""
    out = []
    for c in shorts:
        try:
            if duration is not None and float(c["start"]) >= float(duration):
                continue
            if duration is not None and float(c["end"]) > float(duration):
                c["end"] = float(duration)
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


def cap_total(shorts, duration, pad=None, gap=None, max_total=None, min_keep=1):
    """Drop the lowest-scored clips until the pieces fit ``max_total`` seconds
    (RANGE_MAX_TOTAL_SECONDS, default 1800), but never below ``min_keep``
    clips (the clip-count floor): then it says the cap was exceeded.
    Survivors keep source order."""
    max_total = _env_float("RANGE_MAX_TOTAL_SECONDS", 1800) if max_total is None else float(max_total)
    keep = list(shorts)
    min_keep = max(1, int(min_keep))
    while len(keep) > min_keep and total_seconds(
            merge_ranges(pad_ranges(keep, duration, pad), gap)) > max_total:
        keep = trim_to_best(keep, len(keep) - 1)
    if len(keep) < len(shorts):
        print(f"✂️ Range cap: keeping {len(keep)} of {len(shorts)} clips "
              f"(pieces would exceed {max_total:.0f}s).")
    if total_seconds(merge_ranges(pad_ranges(keep, duration, pad), gap)) > max_total:
        print(f"⚠️ Range cap exceeded: the {min_keep}-clip floor outranks "
              f"the {max_total:.0f}s cap.")
    return keep


# Pieces on disk: <title>.piece_<n>.mp4 (n from 1), plus a sidecar saying which
# range each one holds, so a resumed job reuses a piece only for the same range.
def piece_path(output_dir, title, n):
    return os.path.join(output_dir, f"{title}.piece_{n + 1}.mp4")


def _sidecar(output_dir, title):
    return os.path.join(output_dir, f"{title}.pieces.json")


def _load(output_dir, title):
    try:
        with open(_sidecar(output_dir, title)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _present(output_dir, title, n):
    p = piece_path(output_dir, title, n)
    return os.path.isfile(p) and os.path.getsize(p) > 0


def pending_pieces(output_dir, title, ranges):
    """Indices of ``ranges`` whose piece is missing, empty or from another range."""
    done = _load(output_dir, title)
    out = []
    for n, (s, e) in enumerate(ranges):
        got = done.get(str(n))
        if not (_present(output_dir, title, n) and got
                and abs(got[0] - s) < 0.01 and abs(got[1] - e) < 0.01):
            out.append(n)
    return out


def record_pieces(output_dir, title, ranges, attempted):
    """Note the range of every attempted piece that now exists."""
    done = _load(output_dir, title)
    for n in attempted:
        if _present(output_dir, title, n):
            done[str(n)] = list(ranges[n])
        else:
            done.pop(str(n), None)
    with open(_sidecar(output_dir, title), "w") as f:
        json.dump(done, f)


def pick_piece(pieces, start=None, end=None, tol=0.5):
    """The metadata ``source_pieces`` entry holding ``[start, end]`` (the first
    one when no range is asked for), or None when no single piece does."""
    if not pieces:
        return None
    if start is None:
        return pieces[0]
    end = start if end is None else end
    for p in pieces:
        if p["start"] - tol <= start and end <= p["end"] + tol:
            return p
    return None
