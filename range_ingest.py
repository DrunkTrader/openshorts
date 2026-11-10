"""The post-pick half of a range job: which pieces to fetch, which clips
survive, where each one is cut from, what is cleaned up afterwards.

Light on purpose (no heavy imports, the downloader is passed in) so CI tests
it without the ML stack; main.py supplies ``download``.
"""
import os

import source_ranges as sr


def plan_pieces(shorts, duration, download, min_keep=1):
    """Download the padded, merged ranges of ``shorts`` and return
    ``(kept_shorts, piece_for, source_pieces)``.

    ``download(merged_ranges)`` returns one path (or None for a piece that
    failed) per range. ``piece_for[i]`` is ``(piece_file, piece_start)`` for
    ``kept_shorts[i]``: the clip's time inside the piece is
    ``clip_time - piece_start``. A clip whose piece failed is dropped with a
    log line; the job fails only when none is left."""
    clips = sr.usable(shorts, duration)
    if not clips:
        raise RuntimeError("Clip detection returned no clip with a usable length.")
    clips = sr.cap_total(clips, duration, min_keep=min_keep)
    merged = sr.merge_ranges(sr.pad_ranges(clips, duration))
    print(f"✂️ ranges: {len(merged)} pieces, total {sr.total_seconds(merged):.0f}s "
          f"of {duration:.0f}s (padding {sr._env_float('RANGE_PAD_SECONDS', 10):.0f}s)")
    files = download(merged)
    kept, piece_for = [], {}
    for clip, (n, offset) in zip(clips, sr.assign(clips, merged)):
        if files[n] is None:
            print(f"⚠️ clip dropped ({clip['start']}s-{clip['end']}s): range download failed")
            continue
        piece_for[len(kept)] = (files[n], offset)
        kept.append(clip)
    if not kept:
        raise RuntimeError("No range could be downloaded for any clip.")
    if len(kept) < min(min_keep, len(clips)):
        print(f"⚠️ Only {len(kept)} clip(s) left, below the floor of {min_keep}: "
              f"their pieces could not be downloaded.")
    pieces = [{"file": os.path.basename(f), "start": merged[n][0], "end": merged[n][1]}
              for n, f in enumerate(files) if f]
    return kept, piece_for, pieces


def cleanup(output_dir, audio_path, pieces, keep_source):
    """After a finished job: the audio and the resume sidecar are no longer
    needed. The pieces stay with ``keep_source``: recut cuts from them."""
    doomed = [audio_path] if audio_path else []
    doomed += [os.path.join(output_dir, p["file"].rsplit(".piece_", 1)[0] + ".pieces.json")
               for p in pieces]
    if not keep_source:
        doomed += [os.path.join(output_dir, p["file"]) for p in pieces]
    for f in doomed:
        try:
            os.remove(f)
        except OSError:
            pass
