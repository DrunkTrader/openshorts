"""Clip-level split with two slots, each the clip itself or an uploaded video.

The per-scene SPLIT layout (split_layout.py) stacks two faces of ONE source.
This is the other thing creators ask for: the clip on one half and something
else (a gameplay loop, b-roll) on the other, in a 50/50 or 60/40 stack.

Pure helpers plus ffmpeg argv builders, standard library only (plus the light
split_layout/ffmpeg_utils/layout_ranges), so it imports in CI's thin env. The
face-tracking crop of a ``source`` slot is a clip-level static centre: the
median face over a few sampled frames (per-half tracking is not attempted).

An ``upload`` slot is an extra ffmpeg INPUT (``-stream_loop -1 -ss N -i``).
The graph then references ``[1:v]``, which gpu_render.layers_from_graph cannot
read (``Unsupported``), so the render takes the ffmpeg path by design. A graph
of two source slots stays readable.
"""
import os
import re
import subprocess

import layout_ranges
import split_layout
from ffmpeg_utils import (METADATA_SCRUB, QUALITY_FAST, audio_encode_args,
                          video_encode_args)

RATIOS = {"50/50": 0.5, "60/40": 0.6}
AUDIO_MODES = ("source", "upload", "mix")
KINDS = ("source", "upload")
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_OFFSET_SECONDS = 36000.0
# What the filtergraph may use: the GPU renderer's vocabulary, plus no-ops.
SUPPORTED_FILTERS = frozenset({"crop", "scale", "pad", "vstack", "gblur",
                               "overlay", "setsar", "format", "fps", "trim",
                               "setpts", "split"})


class SlotError(ValueError):
    """Invalid slot request: safe to surface verbatim as a 400 detail."""


def _fraction(value, what):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SlotError(f"{what} must be a number between 0 and 1")
    if not 0.0 <= v <= 1.0:  # also rejects NaN
        raise SlotError(f"{what} must be a number between 0 and 1")
    return v


def _parse_slot(raw, name):
    if raw is None:
        return {"kind": "source"}
    if not isinstance(raw, dict):
        raise SlotError(f"{name} must be an object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise SlotError(f"{name}.kind must be one of: {', '.join(KINDS)}")
    if kind == "source":
        return {"kind": "source"}
    upload_id = raw.get("upload_id")
    if not isinstance(upload_id, str) or not UPLOAD_ID_RE.match(upload_id):
        raise SlotError(f"{name}.upload_id is required for an upload slot")
    try:
        offset = float(raw.get("offset_s", 0) or 0)
    except (TypeError, ValueError):
        raise SlotError(f"{name}.offset_s must be a number")
    if not 0.0 <= offset <= MAX_OFFSET_SECONDS:
        raise SlotError(f"{name}.offset_s must be between 0 and {MAX_OFFSET_SECONDS:.0f}")
    fit = raw.get("fit") or {}
    if not isinstance(fit, dict):
        raise SlotError(f"{name}.fit must be an object")
    return {"kind": "upload", "upload_id": upload_id, "offset_s": offset,
            "fit": {"x": _fraction(fit.get("x", 0.5), f"{name}.fit.x"),
                    "y": _fraction(fit.get("y", 0.5), f"{name}.fit.y")}}


def parse_slots(payload):
    """Validate the request's ``slots`` object into the normalised spec.

    A missing slot is the clip itself; at least one slot must be given. Two
    uploads with ``audio: source`` would leave the clip's sound with nothing to
    carry it, so that case plays the upload's audio instead.
    """
    if not isinstance(payload, dict):
        raise SlotError("slots must be an object")
    if payload.get("top") is None and payload.get("bottom") is None:
        raise SlotError("at least one of top/bottom is required")
    ratio = payload.get("ratio", "50/50")
    if ratio not in RATIOS:
        raise SlotError(f"ratio must be one of: {', '.join(RATIOS)}")
    audio = payload.get("audio", "source")
    if audio not in AUDIO_MODES:
        raise SlotError(f"audio must be one of: {', '.join(AUDIO_MODES)}")
    loop = payload.get("loop", True)
    if not isinstance(loop, bool):
        raise SlotError("loop must be true or false")
    top = _parse_slot(payload.get("top"), "top")
    bottom = _parse_slot(payload.get("bottom"), "bottom")
    if top["kind"] == bottom["kind"] == "upload" and audio == "source":
        audio = "upload"
    return {"top": top, "bottom": bottom, "ratio": ratio, "audio": audio,
            "loop": loop}


def upload_ids(spec):
    """Upload ids in input order (input 1 is the first, 2 the second)."""
    return [s["upload_id"] for s in (spec["top"], spec["bottom"])
            if s["kind"] == "upload"]


def slot_heights(out_h, ratio):
    """(top_h, bottom_h): both even, summing to ``out_h`` (even)."""
    top = int(round(out_h * RATIOS[ratio]))
    top -= top % 2
    return top, out_h - top


def seam_fraction(out_h, ratio):
    """Where the seam sits, as a fraction of the frame height."""
    return slot_heights(out_h, ratio)[0] / float(out_h)


def _upload_chain(in_label, out_label, dims, out_w, slot_h, fit):
    """Cover-scale an upload to the slot, then crop at ``fit`` (0-1 each way)."""
    uw, uh = dims
    scale = max(out_w / float(uw), slot_h / float(uh))
    sw = max(out_w, int(-(-uw * scale // 1)))
    sh = max(slot_h, int(-(-uh * scale // 1)))
    sw += sw % 2
    sh += sh % 2
    x = int(round((sw - out_w) * fit["x"]))
    y = int(round((sh - slot_h) * fit["y"]))
    return (f"[{in_label}]scale={sw}:{sh},crop=w={out_w}:h={slot_h}:x={x}:y={y},"
            f"setsar=1[{out_label}]")


def build_filtergraph(spec, src_w, src_h, out_w, out_h, upload_dims, face_centre):
    """``-filter_complex`` string ending in ``[v]``.

    Input 0 is the source cut; uploads follow in ``upload_ids`` order.
    ``upload_dims`` maps upload id -> (w, h); ``face_centre`` is the (cx, cy)
    the source slots frame. Both-source at 50/50 is exactly
    split_layout.split_filtergraph with one centre for both halves.
    """
    top_h, bot_h = slot_heights(out_h, spec["ratio"])
    kinds = (spec["top"]["kind"], spec["bottom"]["kind"])
    n_source = kinds.count("source")
    chains = []
    if n_source == 2:
        chains.append("[0:v]split=2[ta][ba]")
    next_input = 1
    for name, slot, h in (("top", spec["top"], top_h), ("bot", spec["bottom"], bot_h)):
        if slot["kind"] == "source":
            # split_geometry derives the half from out_h // 2, so ask for a
            # stack twice this slot tall: its half is exactly this slot.
            w, ch, x, y, half = split_layout.split_geometry(
                src_w, src_h, out_w, 2 * h, face_centre)
            src_in = f"{name[0]}a" if n_source == 2 else "0:v"
            chains.append(f"[{src_in}]crop=w={w}:h={ch}:x={x}:y={y},"
                          f"scale={out_w}:{half}[{name}]")
        else:
            chains.append(_upload_chain(
                f"{next_input}:v", name, upload_dims[slot["upload_id"]],
                out_w, h, slot["fit"]))
            next_input += 1
    chains.append(f"[top][bot]vstack=inputs=2,pad={out_w}:{out_h}:0:0,setsar=1[v]")
    return ";".join(chains)


def filter_names(graph):
    """The filter names a graph uses (for the vocabulary test)."""
    names = set()
    for chain in graph.split(";"):
        body = re.sub(r"^(\[[^\]]+\])+|(\[[^\]]+\])+$", "", chain.strip())
        for f in body.split(","):
            names.add(f.strip().partition("=")[0])
    return names


def audio_plan(spec, source_has_audio=True, mix_weights=(1.0, 0.7)):
    """(extra filter_complex chain or None, -map value for the audio).

    ``source``: the cut's own track; ``upload``: the first upload's; ``mix``:
    both through amix, ending with the source's length. The upload's first
    audio input index is 1.
    """
    mode = spec["audio"]
    if not upload_ids(spec):
        mode = "source"
    if mode == "mix" and not source_has_audio:
        mode = "upload"
    if mode == "source":
        return None, "0:a?"
    if mode == "upload":
        return None, "1:a"
    w = " ".join(f"{x:g}" for x in mix_weights)
    return (f"[0:a][1:a]amix=inputs=2:duration=first:weights={w}[a]", "[a]")


def input_args(spec, upload_paths):
    """Extra ``-i`` groups for the uploads, looped / seeked per slot."""
    args = []
    for slot in (spec["top"], spec["bottom"]):
        if slot["kind"] != "upload":
            continue
        if spec["loop"]:
            args += ["-stream_loop", "-1"]
        if slot["offset_s"]:
            args += ["-ss", f"{slot['offset_s']:g}"]
        args += ["-i", upload_paths[slot["upload_id"]]]
    return args


def build_command(spec, cut_path, out_path, upload_paths, upload_dims,
                  src_w, src_h, out_w, out_h, duration, face_centre,
                  source_has_audio=True):
    """Full ffmpeg argv: the cut (input 0), the uploads, one vstack render."""
    graph = build_filtergraph(spec, src_w, src_h, out_w, out_h, upload_dims,
                              face_centre)
    audio_chain, audio_map = audio_plan(spec, source_has_audio)
    if audio_chain:
        graph = f"{graph};{audio_chain}"
    return ["ffmpeg", "-y", "-i", cut_path,
            *input_args(spec, upload_paths),
            "-filter_complex", graph,
            "-map", "[v]", "-map", audio_map,
            # -t, not -shortest: a looped upload never ends.
            "-t", f"{duration:.3f}",
            *video_encode_args(QUALITY_FAST), *audio_encode_args(),
            *METADATA_SCRUB, "-movflags", "+faststart", out_path]


def estimate_face_centre(video_path, src_w, src_h, samples=8):
    """Median (cx, cy) of the biggest face over a few frames, or a centred
    upper-third default when there is none. Heavy libs load only here."""
    fallback = (src_w / 2.0, src_h * 0.4)
    try:
        import cv2
        import numpy as np
        import main as m
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        found = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            faces = m.detect_face_candidates(frame)
            if faces:
                x, y, w, h = max((f["box"] for f in faces), key=lambda b: b[2] * b[3])
                found.append((x + w / 2.0, y + h / 2.0))
        cap.release()
        if found:
            return (float(np.median([c[0] for c in found])),
                    float(np.median([c[1] for c in found])))
    except Exception:
        pass
    return fallback


def probe_video(path):
    """(width, height, duration, has_audio) via ffprobe; SlotError if the file
    has no decodable video stream (the trust boundary for uploads)."""
    import json
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", path], capture_output=True, timeout=60)
        data = json.loads(res.stdout or b"{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SlotError("could not read this file as a video")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("width") and s.get("height")
                  and s.get("codec_name") not in ("mjpeg", "png", "gif")), None)
    if res.returncode != 0 or not video:
        raise SlotError("this file is not a video")
    try:
        duration = float((data.get("format") or {}).get("duration")
                         or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return int(video["width"]), int(video["height"]), duration, has_audio


def seam_range(duration, out_h, ratio):
    """The single layout range recorded for a slot split (whole clip), which
    keeps captions on the seam through restyles and recuts."""
    seam = seam_fraction(out_h, ratio)
    return [(0.0, duration, "split", None, seam)]


def write_layout(out_path, duration, out_h, ratio):
    return layout_ranges.write(out_path, seam_range(duration, out_h, ratio))


def run_ffmpeg(command, timeout=1800):
    try:
        res = subprocess.run(command, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s")
    if res.returncode != 0:
        tail = (res.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg failed: {tail}")


def basename_safe(name):
    return os.path.basename(str(name or "").replace("\\", "/")) or "upload"


def perform_split(*, source_path, segments, spec, uploads, output_dir, clean_name,
                  out_size, captions_transcript=None, captioner=None,
                  face_centre_fn=None, runner=None):
    """Render a slot split. Returns (served_filename, clean_filename).

    ``source_path``/``segments``: the source and the times ON THAT FILE (the
    caller rebases). ``uploads``: id -> {path, width, height, has_audio}.
    ``out_size``: (w, h), the job's delivery size. Naming follows
    recut.perform_recut (``recut_<ts>_<hex>_<clean>``) so the walk-backs that
    resolve a clip's newest derivative keep working; captions are burned last
    onto a ``subtitled_`` derivative, leaving the clean file clean.
    """
    import time
    import uuid
    import recut

    run = runner or run_ffmpeg
    out_w, out_h = out_size
    duration = recut.total_duration(segments)
    out_name = f"recut_{int(time.time())}_{uuid.uuid4().hex[:6]}_{clean_name}"
    out_path = os.path.join(output_dir, out_name)
    cut_path = os.path.join(output_dir, f"temp_{out_name}")
    try:
        recut.run_cut_concat(source_path, segments, cut_path, output_dir, runner=runner)
        src_w, src_h, _d, has_audio = probe_video(cut_path)
        centre = (src_w / 2.0, src_h * 0.4)
        if "source" in (spec["top"]["kind"], spec["bottom"]["kind"]):
            centre = (face_centre_fn or estimate_face_centre)(cut_path, src_w, src_h)
        cmd = build_command(
            spec, cut_path, out_path,
            {i: u["path"] for i, u in uploads.items()},
            {i: (u["width"], u["height"]) for i, u in uploads.items()},
            src_w, src_h, out_w, out_h, duration, centre, source_has_audio=has_audio)
        run(cmd)
        write_layout(out_path, duration, out_h, spec["ratio"])
        served = out_name
        if captions_transcript and captions_transcript.get("segments"):
            if captioner is None:
                import main
                captioner = main.auto_caption_clip
            captioned = captioner(out_path, captions_transcript, 0.0, duration)
            if captioned:
                served = os.path.basename(captioned)
        return served, out_name
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
