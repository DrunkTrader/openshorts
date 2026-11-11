"""Per-owner library of uploaded clips used as slot-split halves.

Files live in ``<video_dir>/<id>.<ext>`` (never served), thumbnails in
``<thumb_dir>/<id>.jpg`` (served by the authorised /thumbnails mount), and a
JSON index in the video dir. No database table: account erasure removes both
directories (app._purge_local_jobs_for_user).
"""
import os
import re
import subprocess
import time
import uuid

from jsonfile import write_json_atomic

ALLOWED_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv")
INDEX = "index.json"
_OWNER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def owner_key(user_id):
    """Directory-safe owner name: the user's id, or ``local`` for self-host."""
    if user_id is None:
        return "local"
    key = str(user_id)
    if not _OWNER_RE.match(key):
        raise ValueError("unsafe owner id")
    return key


def sniff_container(path):
    """True when the first bytes are an MP4/MOV (``ftyp``) or Matroska/WebM
    (EBML) header: a cheap check before ffprobe, so a text file named .mp4
    never reaches a demuxer."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3"


def _read(video_dir):
    import json
    try:
        with open(os.path.join(video_dir, INDEX)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_items(video_dir):
    items = [dict(v, id=k) for k, v in _read(video_dir).items()]
    return sorted(items, key=lambda i: i.get("created", 0), reverse=True)


def get(video_dir, item_id):
    entry = _read(video_dir).get(item_id)
    if not entry:
        return None
    return dict(entry, id=item_id, path=os.path.join(video_dir, entry["file"]))


def add(video_dir, thumb_dir, tmp_path, ext, name, info):
    """Move a validated upload in and index it. ``info``: width, height,
    duration, has_audio."""
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)
    item_id = uuid.uuid4().hex
    file = f"{item_id}{ext}"
    os.replace(tmp_path, os.path.join(video_dir, file))
    thumb = make_thumb(os.path.join(video_dir, file),
                       os.path.join(thumb_dir, f"{item_id}.jpg"), info["duration"])
    index = _read(video_dir)
    index[item_id] = {"file": file, "name": name, "created": time.time(),
                      "thumb": f"{item_id}.jpg" if thumb else None, **info}
    write_json_atomic(os.path.join(video_dir, INDEX), index)
    return dict(index[item_id], id=item_id)


def delete(video_dir, thumb_dir, item_id):
    index = _read(video_dir)
    entry = index.pop(item_id, None)
    if not entry:
        return False
    for path in (os.path.join(video_dir, entry["file"]),
                 os.path.join(thumb_dir, f"{item_id}.jpg")):
        try:
            os.remove(path)
        except OSError:
            pass
    write_json_atomic(os.path.join(video_dir, INDEX), index)
    return True


def make_thumb(video_path, thumb_path, duration):
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.0, duration / 2):.2f}",
             "-i", video_path, "-frames:v", "1", "-vf", "scale=240:-2", thumb_path],
            capture_output=True, timeout=60)
        return res.returncode == 0 and os.path.exists(thumb_path)
    except (OSError, subprocess.SubprocessError):
        return False
