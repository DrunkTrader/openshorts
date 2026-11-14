"""Telegram intake bot (self-host): send a link or a video, get clips back.

Long polling over httpx, no bot framework. It talks to OpenShorts only through
the loopback HTTP API, so every API guard (SSRF check, acknowledgement, rate
limits, signed media URLs, owner checks) stays in force. Separate from the
admin-alert bot in cloud/alerts.py: its own token, never shared.
See docs/telegram-bot.md.
"""
import asyncio
import json
import logging
import os
import random
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

import jsonfile
from hooks import HOOK_STYLES
from subtitles import CAPTION_PRESETS

log = logging.getLogger("telegram_bot")

TG = "https://api.telegram.org"
FILE_LIMIT = 20 * 1024 * 1024    # Bot API: getFile downloads
SEND_LIMIT = 50 * 1024 * 1024    # Bot API: uploads
PLATFORMS = ("tiktok", "instagram", "youtube")
# State file sections besides offset/jobs (docs/telegram-bot.md has the shape).
STATE_KEYS = ("panels", "presets", "default_preset", "awaiting", "job_settings", "clips")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

HELP = (
    "Send a YouTube (or other video) link and I will show a settings panel; press Start "
    "to queue it, and I send the clips back here.\n"
    "You can also send a video file up to 20 MB; for bigger videos send a link.\n\n"
    "/library - the split-screen videos, with delete buttons (imports the server folder first)\n"
    "Send a video with the caption /save <name> to add it to the library.\n"
    "/presets - list saved presets\n"
    "/preset_delete <name> - delete a preset\n"
    "/preset_default <name> - the preset new panels start from\n"
    "/schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM> - schedule a clip "
    "(platforms: tiktok,instagram,youtube; job is the short id from Queued)\n"
    "/jobs - queued and running jobs, each with a 🛑 cancel button\n"
    "/clips [job] - resend a finished job's clips with their buttons (latest job if omitted)\n"
    "🗓 Schedule upload (under each clip) - send it to YouTube later; times are in "
    "TELEGRAM_TIMEZONE\n"
    "/scheduled - list scheduled YouTube uploads (each with ❌ cancel) and Upload-Post posts\n"
    "/cancel <id> - cancel a scheduled post (to cancel a job, use /jobs or its 🛑 button)\n"
    "/help - this text"
)
NO_UPLOAD = ("\n\nUpload to YouTube is off: N8N_UPLOAD_WEBHOOK_URL is not set on the server, "
             "so clips come without approval buttons.")

# Panel settings: short key -> (label, choices). Values go to /api/process as-is
# (see form_fields); privacy only travels to the n8n upload.
OPTIONS = {
    "cap": ("Captions", ("off", *CAPTION_PRESETS)),
    "lay": ("Layout", ("auto", "none", "split", "screencast", "speaker_cut")),  # app.layout_env
    "fmt": ("Format", ("vertical", "square", "horizontal")),
    "hook": ("Hook", ("off", *HOOK_STYLES)),
    "n": ("Clips", ("auto", "3", "5", "8", "10")),
    "len": ("Length", ("30-60", "15-30", "30-90", "60-90")),
    "priv": ("YouTube privacy", ("private", "unlisted", "public")),
}
# Split screen (spl): the clip and an extra video from the library, stacked by
# /api/clip/split once the job is done. "src" is {"id", "name"} of a library item.
SPLIT_OPTIONS = {
    "pos": ("Extra video at", ("bottom", "top")),
    "rat": ("Ratio", ("50/50", "60/40")),        # 60 is always the main clip
    "aud": ("Audio", ("source", "extra", "mix")),
    "off": ("Start offset", ("0", "10", "30", "60")),  # or typed seconds
}
DEFAULTS = {"cap": "default", "lay": "auto", "fmt": "vertical", "hook": "pill",
            "n": "auto", "len": "30-60", "priv": "private",
            "spl": "off", "pos": "bottom", "rat": "50/50", "aud": "source", "off": "0", "src": None}
NAME_BYTES = 32   # preset names ride in callback_data ("l:<name>", 64-byte cap)
TITLE_CHARS = 100
MAX_OFFSET = 36000   # slot_split.MAX_OFFSET_SECONDS
_ID_RE = re.compile(r"^[0-9a-f]{32}$")   # a library item id
_JOB_RE = re.compile(r"^[A-Za-z0-9-]{1,48}$")
MAX_AHEAD = timedelta(days=60)      # scheduled uploads: furthest ahead
LATE_AFTER = timedelta(minutes=2)   # a due upload started later than this says it ran late
TICK_SECONDS = 30                   # scheduler check interval
# Quick choices of the schedule menu: code -> (label, days from today, hour); "h" is now + 1 h.
QUICK = {"h": ("in 1 hour", None, None), "e": ("today 20:00", 0, 20),
         "m": ("tomorrow 09:00", 1, 9), "a": ("tomorrow 18:00", 1, 18)}


def parse_offset(text) -> Optional[str]:
    """Seconds as a short string (0 <= s <= MAX_OFFSET), else None."""
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return f"{v:g}" if 0 <= v <= MAX_OFFSET else None   # also rejects NaN


def clean_settings(s) -> dict:
    """Every key present with a value the current code offers (old presets may name
    a style that is gone). Split on forces vertical: the split renders 9:16 only."""
    s = s if isinstance(s, dict) else {}
    out = {k: s.get(k) if s.get(k) in choices else DEFAULTS[k]
           for k, (_, choices) in {**OPTIONS, **SPLIT_OPTIONS}.items()}
    out["off"] = parse_offset(s.get("off")) or "0"
    src = s.get("src")
    out["src"] = ({"id": src["id"], "name": str(src.get("name") or "video")[:64]}
                  if isinstance(src, dict) and isinstance(src.get("id"), str)
                  and _ID_RE.match(src["id"]) else None)
    out["spl"] = "on" if s.get("spl") == "on" else "off"
    if out["spl"] == "on":
        out["fmt"] = "vertical"
    return out


def split_body(job_id, i, s) -> dict:
    """The /api/clip/split body for clip ``i`` with split settings ``s``."""
    extra = {"kind": "upload", "upload_id": s["src"]["id"], "offset_s": float(s["off"])}
    top, bottom = (extra, {"kind": "source"}) if s["pos"] == "top" else ({"kind": "source"}, extra)
    # The ratio is the top slot's share: 60/40 keeps the 60 on the main clip.
    ratio = "50/50" if s["rat"] == "50/50" else ("40/60" if s["pos"] == "top" else "60/40")
    return {"job_id": job_id, "clip_index": i,
            "slots": {"top": top, "bottom": bottom, "ratio": ratio,
                      "audio": {"extra": "upload"}.get(s["aud"], s["aud"]), "loop": True}}


def form_fields(s) -> dict:
    """The /api/process form fields for panel settings."""
    lo, hi = s["len"].split("-")
    f = {"output_format": s["fmt"], "layouts": s["lay"],
         "clip_min_seconds": lo, "clip_max_seconds": hi,
         "captions": "false" if s["cap"] == "off" else s["cap"],
         "auto_hook": "false" if s["hook"] == "off" else "true"}
    if s["hook"] != "off":
        f["auto_hook_style"] = s["hook"]
    if s["n"] != "auto":
        f["target_clips"] = s["n"]
    return f


def preset_name(text: str) -> str:
    """Letters, digits, space, _ and -; at most NAME_BYTES bytes of UTF-8."""
    name = " ".join(re.sub(r"[^\w \-]+", "", text or "").split())
    return name.encode()[:NAME_BYTES].decode("utf-8", "ignore").strip()


def _label(v) -> str:
    return {"none": "plain crop"}.get(v, str(v).replace("_", " "))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _btn(text, data):
    return {"text": text, "callback_data": data}


def _rows(buttons, per):
    return [buttons[i:i + per] for i in range(0, len(buttons), per)]


def enabled(env, billing: bool) -> bool:
    """Self-host only, with a token and a non-empty allowlist (fail closed)."""
    return (not billing and bool(env.get("TELEGRAM_INTAKE_TOKEN"))
            and bool(_parse_ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))))


def _parse_ids(raw: str) -> set:
    out = set()
    for p in (raw or "").split(","):
        try:
            out.add(int(p.strip()))
        except ValueError:
            pass
    return out


def parse_command(text: str):
    """('/cmd', 'rest') for a slash command (bot suffix stripped), else None."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    return head.split("@")[0].lower(), rest.strip()


def extract_url(text: str) -> Optional[str]:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)") if m else None


def _zone(tz: str):
    """(tzinfo, name actually used): UTC when the zone database has no such zone
    (Windows without tzdata)."""
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz), tz
    except Exception:
        return timezone.utc, "UTC"


def parse_when(text: str, tz: str = "UTC"):
    """(aware datetime, tz name actually used). ValueError on bad text."""
    naive = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    zone, used = _zone(tz)
    return naive.replace(tzinfo=zone), used


def quick_time(code, now, tz="UTC") -> Optional[datetime]:
    """The UTC instant of a schedule-menu quick choice, days counted in ``tz``."""
    if code not in QUICK:
        return None
    _, days, hour = QUICK[code]
    if days is None:
        return now + timedelta(hours=1)
    zone, _ = _zone(tz)
    d = now.astimezone(zone).date() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, tzinfo=zone).astimezone(timezone.utc)


def check_when(when, now) -> Optional[str]:
    """Why ``when`` cannot be a scheduled upload time, or None."""
    if when <= now:
        return "That time is in the past."
    if when > now + MAX_AHEAD:
        return "That is more than 60 days away."
    return None


class IntakeBot:
    def __init__(self, api_base, token, allowed_chat_ids, client_factory, state_path,
                 tz="UTC", upload_key="", profile="", send_files=True, public_url="",
                 n8n_url="", n8n_secret=""):
        self.api = api_base.rstrip("/")
        self.token = token
        self.allowed = set(allowed_chat_ids)
        self.client_factory = client_factory
        self.state_path = state_path
        self.tz, self.upload_key, self.profile = tz, upload_key, profile
        self.send_files, self.public_url = send_files, public_url.rstrip("/")
        self.n8n_url, self.n8n_secret = n8n_url, n8n_secret
        self.sleep = asyncio.sleep
        self.tick_sleep = asyncio.sleep               # scheduler loop (tests inject both)
        self.now = lambda: datetime.now(timezone.utc)
        self.offset = 0
        self.jobs: dict = {}      # str(chat_id) -> last 20 job ids
        self.data = {k: {} for k in STATE_KEYS}
        self.ignored = 0
        self._tasks: set = set()
        self._load()

    # --- plumbing -------------------------------------------------------
    def _safe(self, e) -> str:
        out = str(e)
        for secret in (self.token, self.n8n_secret, self.n8n_url):
            if secret:
                out = out.replace(secret, "***")
        return out

    def _load(self):
        try:
            with open(self.state_path) as f:
                s = json.load(f)
            self.offset = int(s.get("offset", 0))
            self.jobs = {str(k): list(v) for k, v in s.get("jobs", {}).items()}
            self.data.update({k: dict(s.get(k) or {}) for k in STATE_KEYS})
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            for key, cap in (("job_settings", 200), ("clips", 500)):
                for old in list(self.data[key])[:-cap]:
                    if self.data[key][old].get("state") != "scheduled":   # never drop a pending upload
                        del self.data[key][old]
            jsonfile.write_json_atomic(self.state_path,
                                       {"offset": self.offset, "jobs": self.jobs, **self.data})
        except Exception as e:
            log.warning("telegram state not saved: %s", self._safe(e))

    async def _tg(self, c, method, **kw):
        """Bot API call; returns the `result` or None (logged, token redacted)."""
        try:
            r = await c.post(f"{TG}/bot{self.token}/{method}", **kw)
            body = r.json()
            if not body.get("ok"):
                log.warning("telegram %s refused: %s", method, body.get("description", r.status_code))
                return None
            return body.get("result")
        except Exception as e:
            log.warning("telegram %s failed: %s", method, self._safe(e))
            return None

    async def _say(self, c, chat_id, text, markup=None):
        body = {"chat_id": chat_id, "text": text[:4000]}
        if markup is not None:
            body["reply_markup"] = {"inline_keyboard": markup}
        r = await self._tg(c, "sendMessage", json=body)
        return r.get("message_id") if isinstance(r, dict) else None

    async def _edit(self, c, chat_id, msg_id, text, markup=None):
        """editMessageText; `markup` None leaves no buttons, a list sets them."""
        if msg_id is None:
            return await self._say(c, chat_id, text, markup)
        await self._tg(c, "editMessageText",
                       json={"chat_id": chat_id, "message_id": msg_id, "text": text[:4000],
                             "reply_markup": {"inline_keyboard": markup or []}})
        return msg_id

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _remember(self, chat_id, job_id):
        lst = self.jobs.setdefault(str(chat_id), [])
        if job_id in lst:
            lst.remove(job_id)
        lst.append(job_id)
        del lst[:-20]
        self._save()

    def _resolve(self, chat_id, short):
        for j in reversed(self.jobs.get(str(chat_id), [])):
            if j.startswith(short):
                return j
        return None

    async def _api(self, c, method, path, **kw):
        """(status, json-or-{}) from the loopback API; (0, {}) on network error."""
        try:
            r = await c.request(method, f"{self.api}{path}", **kw)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {}
        except Exception as e:
            log.warning("api %s failed: %s", path, self._safe(e))
            return 0, {}

    @staticmethod
    def _detail(body) -> str:
        d = body.get("detail") if isinstance(body, dict) else None
        return d if isinstance(d, str) else "the server refused it"

    # --- updates --------------------------------------------------------
    async def handle_update(self, update):
        if "callback_query" in update:
            return await self.handle_callback(update["callback_query"])
        msg = update.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        user_id = (msg.get("from") or {}).get("id")
        # Both, as for callbacks: in a listed group an unlisted member gets nothing.
        if chat_id not in self.allowed or user_id not in self.allowed:
            self.ignored += 1
            log.info("ignored update from unlisted chat/user (%d so far)", self.ignored)
            return
        async with self.client_factory() as c:
            try:
                await self._dispatch(c, chat_id, msg)
            except Exception as e:
                log.warning("update failed: %s", self._safe(e))
                await self._say(c, chat_id, "Something went wrong with that message.")

    def _help(self):
        return HELP + ("" if self.n8n_url else NO_UPLOAD)

    async def _dispatch(self, c, chat_id, msg):
        text = msg.get("text") or ""
        cmd = parse_command(text)
        doc = msg.get("video") or msg.get("document")
        if doc and not (msg.get("video") or str(doc.get("mime_type", "")).startswith("video/")):
            doc = None
        # Any message ends a pending prompt; plain text (not a command or a link)
        # answers it, a video answers "send a video", a link "send a link".
        waiting = self.data["awaiting"].pop(str(chat_id), None)
        if waiting:
            self._save()
            kind = waiting.get("kind")
            if kind == "split_video" and doc:
                return await self._split_video(c, chat_id, waiting, doc)
            if kind == "split_link" and text and not cmd and extract_url(text):
                return await self._split_link(c, chat_id, waiting, extract_url(text))
            if kind not in ("split_video", "split_link") and text and not cmd and not extract_url(text):
                return await self._answer_prompt(c, chat_id, waiting, text)
        if doc:
            if int(doc.get("file_size") or 0) > FILE_LIMIT:
                return await self._say(c, chat_id, "That file is over 20 MB, which is the most a bot can "
                                                   "download. Send a link instead.")
            caption = parse_command(msg.get("caption") or "")
            if caption and caption[0] == "/save":
                return await self._save_video(c, chat_id, doc, caption[1])
            return await self._open_panel(c, chat_id, {"file": {
                k: doc.get(k) for k in ("file_id", "file_name", "file_size")}})
        if cmd:
            name, rest = cmd
            fn = {"/schedule": self._schedule, "/scheduled": self._scheduled,
                  "/cancel": self._cancel, "/presets": self._presets,
                  "/preset_delete": self._preset_delete,
                  "/preset_default": self._preset_default,
                  "/library": self._library, "/jobs": self._jobs,
                  "/clips": self._clips}.get(name)
            if fn:
                return await fn(c, chat_id, rest)
            return await self._say(c, chat_id, self._help())
        url = extract_url(text)
        if not url:
            return await self._say(c, chat_id, self._help())
        await self._open_panel(c, chat_id, {"url": url})

    # --- settings panel -------------------------------------------------
    def _panels(self, chat_id) -> dict:
        return self.data["panels"].setdefault(str(chat_id), {})

    def _presets_of(self, chat_id) -> dict:
        return self.data["presets"].setdefault(str(chat_id), {})

    def _start_settings(self, chat_id) -> dict:
        name = self.data["default_preset"].get(str(chat_id))
        return clean_settings(self._presets_of(chat_id).get(name) or DEFAULTS)

    @staticmethod
    def panel_text(panel) -> str:
        src = panel["src"]
        what = src.get("url") or (src.get("file") or {}).get("file_name") or "uploaded video"
        s = panel["s"]
        lines = [f"{label}: {_label(s[k])}" for k, (label, _) in OPTIONS.items()]
        if s["spl"] == "on":
            lines[list(OPTIONS).index("fmt")] += " (split screen needs it)"
        lines.append(IntakeBot.split_line(s))
        note = f"\n\n⚠️ {panel['note']}" if panel.get("note") else ""
        return f"New job: {what}\n\n" + "\n".join(lines) + note + "\n\nPick options, then press Start."

    @staticmethod
    def split_line(s) -> str:
        if s["spl"] != "on":
            return "Split screen: off"
        name = s["src"]["name"] if s["src"] else "not picked yet"
        return (f"Split screen: on, extra video {name} at the {s['pos']}, {s['rat']} "
                f"(the bigger part is the clip), audio {s['aud']}, from {s['off']} s")

    @staticmethod
    def panel_keyboard(s):
        opts = [_btn(f"{label}: {_label(s[k])}", f"m:{k}") for k, (label, _) in OPTIONS.items()]
        opts.append(_btn(f"Split screen: {s['spl']}", "m:spl"))
        return _rows(opts, 2) + [[_btn("▶️ Start", "go"), _btn("💾 Save as preset", "sv")],
                                 [_btn("📂 Load preset", "ld"), _btn("✖️ Cancel", "x")]]

    @staticmethod
    def option_keyboard(s, key):
        opts = {**OPTIONS, **SPLIT_OPTIONS}
        choices = [_btn(("✓ " if v == s[key] else "") + _label(v), f"s:{key}:{v}")
                   for v in opts[key][1]]
        if key == "off":
            choices.append(_btn("⌨️ Type seconds", "sw:o"))
        return _rows(choices, 3) + [[_btn("« Back", "m:spl" if key in SPLIT_OPTIONS else "m:")]]

    @staticmethod
    def split_keyboard(s):
        on = s["spl"] == "on"
        rows = [[_btn(("✓ " if not on else "") + "Off", "s:spl:off"),
                 _btn(("✓ " if on else "") + "On (vertical only)", "s:spl:on")],
                [_btn(f"Extra video: {s['src']['name'] if s['src'] else 'pick one'}", "m:src")]]
        opts = [_btn(f"{label}: {s[k]}", f"m:{k}") for k, (label, _) in SPLIT_OPTIONS.items()]
        return rows + _rows(opts, 2) + [[_btn("« Back", "m:")]]

    @staticmethod
    def source_keyboard():
        return [[_btn("📚 Library", "sl")], [_btn("📤 Send a video", "sw:v")],
                [_btn("🔗 Send a link", "sw:u")], [_btn("« Back", "m:spl")]]

    @staticmethod
    def pick_keyboard(items):
        return [[_btn(str(i.get("name") or "video")[:40], f"sp:{i['id']}")] for i in items
                if _ID_RE.match(str(i.get("id")))] + [[_btn("« Back", "m:src")]]

    @staticmethod
    def library_keyboard(items):
        return [[_btn(f"🗑 {str(i.get('name') or 'video')[:40]}", f"lx:{i['id']}")] for i in items
                if _ID_RE.match(str(i.get("id")))]

    def presets_keyboard(self, chat_id):
        names = sorted(self._presets_of(chat_id))
        return _rows([_btn(n, f"l:{n}") for n in names], 2) + [[_btn("« Back", "m:")]]

    async def _library_items(self, c):
        """(items, None) from the API, or (None, reason)."""
        status, body = await self._api(c, "GET", "/api/slot-uploads")
        if status != 200:
            return None, "OpenShorts is not reachable" if status == 0 else self._detail(body)
        return [i for i in body.get("items") or [] if isinstance(i, dict)], None

    async def _check_split_src(self, c, panel):
        """A preset may name a library video deleted since: split off for this run."""
        s = panel["s"]
        panel.pop("note", None)
        if s["spl"] != "on" or not s["src"]:
            return
        items, _err = await self._library_items(c)
        if items is not None and s["src"]["id"] not in {i.get("id") for i in items}:
            s["spl"] = "off"
            panel["note"] = (f"The split-screen video {s['src']['name']} is no longer in the "
                             "library, so split screen is off for this run.")

    async def _open_panel(self, c, chat_id, src):
        panel = {"src": src, "s": self._start_settings(chat_id)}
        await self._check_split_src(c, panel)
        mid = await self._say(c, chat_id, self.panel_text(panel), self.panel_keyboard(panel["s"]))
        if mid is None:
            return
        panels = self._panels(chat_id)
        panels[str(mid)] = panel
        for old in list(panels)[:-10]:
            del panels[old]
        self._save()

    async def _redraw(self, c, chat_id, mid, panel, markup=None):
        await self._edit(c, chat_id, mid, self.panel_text(panel), markup or self.panel_keyboard(panel["s"]))

    async def _keys(self, c, chat_id, mid, markup):
        await self._tg(c, "editMessageReplyMarkup", json={
            "chat_id": chat_id, "message_id": mid, "reply_markup": {"inline_keyboard": markup}})

    # --- callbacks ------------------------------------------------------
    async def handle_callback(self, cq):
        m = cq.get("message") or {}
        chat_id = (m.get("chat") or {}).get("id")
        user_id = (cq.get("from") or {}).get("id")
        # Both: in a group, the chat being listed must not let any member press.
        if chat_id not in self.allowed or user_id not in self.allowed:
            self.ignored += 1
            log.info("ignored callback from unlisted chat/user (%d so far)", self.ignored)
            return
        async with self.client_factory() as c:
            try:
                note = await self._on_callback(c, chat_id, m.get("message_id"), cq.get("data") or "")
            except Exception as e:
                log.warning("callback failed: %s", self._safe(e))
                note = "Something went wrong."
            body = {"callback_query_id": cq.get("id")}
            if note:
                body["text"] = note[:190]
            await self._tg(c, "answerCallbackQuery", json=body)

    async def _on_callback(self, c, chat_id, mid, data):
        """Returns the toast text for answerCallbackQuery (or None)."""
        head, _, arg = data.partition(":")
        if head in ("u", "k", "t", "q", "qa", "qt", "qb", "qy", "qn", "qc", "qx"):
            return await self._clip_action(c, chat_id, mid, head, arg)
        if head == "ow":
            return await self._confirm_overwrite(c, chat_id, mid, arg == "y")
        if head == "lx":
            return await self._library_delete(c, chat_id, mid, arg)
        if head in ("cj", "cy", "cn"):
            return await self._job_cancel(c, chat_id, mid, head, arg)
        panel = self._panels(chat_id).get(str(mid))
        if panel is None:
            return "This panel has expired. Send the link again."
        s = panel["s"]
        if head == "m":
            if arg in OPTIONS or arg in SPLIT_OPTIONS:
                kb = self.option_keyboard(s, arg)
            else:
                kb = {"spl": lambda: self.split_keyboard(s),
                      "src": self.source_keyboard}.get(arg, lambda: self.panel_keyboard(s))()
            await self._keys(c, chat_id, mid, kb)
        elif head == "s":
            key, _, val = arg.partition(":")
            if key == "spl" and val in ("off", "on"):
                s["spl"] = val
                if val == "on":
                    s["fmt"] = "vertical"
                panel.pop("note", None)
            elif key == "fmt" and s["spl"] == "on" and val != "vertical":
                return "Split screen needs vertical. Turn split screen off first."
            elif key in OPTIONS and val in OPTIONS[key][1] or key in SPLIT_OPTIONS and val in SPLIT_OPTIONS[key][1]:
                s[key] = val
            else:
                return None
            self._save()
            await self._redraw(c, chat_id, mid, panel,
                               self.split_keyboard(s) if key == "spl" or key in SPLIT_OPTIONS else None)
        elif head == "sl":
            items, err = await self._library_items(c)
            if items is None:
                return f"Could not read the library: {err}"
            if not items:
                return "The library is empty. Use Send a video or Send a link."
            await self._keys(c, chat_id, mid, self.pick_keyboard(items))
        elif head == "sp":
            items, err = await self._library_items(c)
            item = next((i for i in items or [] if i.get("id") == arg), None)
            if item is None:
                return f"Could not read the library: {err}" if err else "That video is gone from the library."
            s["src"] = {"id": arg, "name": str(item.get("name") or "video")[:64]}
            self._save()
            await self._redraw(c, chat_id, mid, panel, self.split_keyboard(s))
            return f"Picked {s['src']['name']}."
        elif head == "sw":
            kind, prompt = {
                "v": ("split_video", "Send the extra video now, as a video or a file. A bot can download "
                                     "at most 20 MB; for a bigger one use Send a link. It is saved in "
                                     "your library (see /library)."),
                "u": ("split_link", "Send the link of the extra video. The server downloads the start "
                                    "of it into your library (see /library)."),
                "o": ("split_offset", "Send the start offset in the extra video, in seconds (0 or more)."),
            }.get(arg, (None, None))
            if kind:
                self.data["awaiting"][str(chat_id)] = {"kind": kind, "panel": str(mid)}
                self._save()
                await self._say(c, chat_id, prompt)
        elif head == "ld":
            if not self._presets_of(chat_id):
                return "No presets yet. Use Save as preset first."
            await self._keys(c, chat_id, mid, self.presets_keyboard(chat_id))
        elif head == "l":
            saved = self._presets_of(chat_id).get(arg)
            if saved is None:
                return "That preset is gone."
            panel["s"] = clean_settings(saved)
            await self._check_split_src(c, panel)
            self._save()
            await self._redraw(c, chat_id, mid, panel)
            return f"Loaded {arg}."
        elif head == "sv":
            self.data["awaiting"][str(chat_id)] = {"kind": "name", "panel": str(mid)}
            self._save()
            await self._say(c, chat_id, f"Send a name for this preset (letters, digits, spaces, "
                                        f"up to {NAME_BYTES} characters).")
        elif head == "x":
            self._panels(chat_id).pop(str(mid), None)
            self._save()
            await self._edit(c, chat_id, mid, "Cancelled.")
        elif head == "go":
            if s["spl"] == "on" and not s["src"]:
                return "Pick the extra video for the split screen first, or turn split screen off."
            self._panels(chat_id).pop(str(mid), None)
            self._save()
            await self._edit(c, chat_id, mid, self.panel_text(panel).rsplit("\n\n", 1)[0]
                             + "\n\nStarting...")
            await self._start(c, chat_id, panel)
        return None

    async def _start(self, c, chat_id, panel):
        fields, src = form_fields(panel["s"]), panel["src"]
        if "url" in src:
            status, body = await self._api(c, "POST", "/api/process",
                                           data={"url": src["url"], "acknowledged": "true", **fields})
            return await self._queued(c, chat_id, status, body, panel["s"])
        await self._from_file(c, chat_id, src["file"], fields, panel["s"])

    # --- presets --------------------------------------------------------
    async def _answer_prompt(self, c, chat_id, waiting, text):
        if waiting.get("kind") == "title":
            return await self._set_title(c, chat_id, waiting.get("key"), text)
        if waiting.get("kind") == "when":
            return await self._typed_when(c, chat_id, waiting.get("key"), text)
        panel = self._panels(chat_id).get(waiting.get("panel") or "")
        if panel is None:
            return await self._say(c, chat_id, "That panel is gone; nothing saved.")
        if waiting.get("kind") == "split_offset":
            off = parse_offset(text)
            if off is None:
                return await self._say(c, chat_id, "That is not a number of seconds (0 or more). "
                                                   "Press Type seconds again.")
            panel["s"]["off"] = off
            self._save()
            await self._redraw(c, chat_id, int(waiting["panel"]), panel, self.split_keyboard(panel["s"]))
            return await self._say(c, chat_id, f"Start offset set to {off} s.")
        name = preset_name(text)
        if not name:
            return await self._say(c, chat_id, "That name is empty once cleaned. Press Save as preset again.")
        if name in self._presets_of(chat_id):
            self.data["awaiting"][str(chat_id)] = {"kind": "overwrite", "name": name,
                                                   "s": dict(panel["s"])}
            self._save()
            return await self._say(c, chat_id, f"A preset named {name} exists. Overwrite it?",
                                   [[_btn("Yes, overwrite", "ow:y"), _btn("No", "ow:n")]])
        self._presets_of(chat_id)[name] = dict(panel["s"])
        self._save()
        await self._say(c, chat_id, f"Saved preset {name}.")

    async def _confirm_overwrite(self, c, chat_id, mid, yes):
        waiting = self.data["awaiting"].get(str(chat_id)) or {}
        if waiting.get("kind") != "overwrite":
            return "Nothing to confirm."
        del self.data["awaiting"][str(chat_id)]
        if yes:
            self._presets_of(chat_id)[waiting["name"]] = clean_settings(waiting["s"])
        self._save()
        await self._edit(c, chat_id, mid, f"Saved preset {waiting['name']}." if yes
                         else f"Kept the old {waiting['name']}.")
        return None

    async def _presets(self, c, chat_id, rest):
        names = sorted(self._presets_of(chat_id))
        if not names:
            return await self._say(c, chat_id, "No presets yet. Send a link and use Save as preset.")
        default = self.data["default_preset"].get(str(chat_id))
        await self._say(c, chat_id, "Presets:\n" + "\n".join(
            f"{n}{' (default)' if n == default else ''}" for n in names))

    async def _preset_delete(self, c, chat_id, rest):
        name = preset_name(rest)
        if self._presets_of(chat_id).pop(name, None) is None:
            return await self._say(c, chat_id, "No preset with that name (see /presets).")
        if self.data["default_preset"].get(str(chat_id)) == name:
            del self.data["default_preset"][str(chat_id)]
        self._save()
        await self._say(c, chat_id, f"Deleted preset {name}.")

    async def _preset_default(self, c, chat_id, rest):
        name = preset_name(rest)
        if name not in self._presets_of(chat_id):
            return await self._say(c, chat_id, "No preset with that name (see /presets).")
        self.data["default_preset"][str(chat_id)] = name
        self._save()
        await self._say(c, chat_id, f"New panels start from {name}.")

    # --- intake ---------------------------------------------------------
    async def _tg_download(self, c, doc):
        """(temp path, None) of a Telegram file (the caller deletes it), or (None, reason)."""
        info = await self._tg(c, "getFile", json={"file_id": doc.get("file_id")})
        path = (info or {}).get("file_path")
        if not path:
            return None, "Could not fetch that file from Telegram. Send a link instead."
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".mp4")
        err = "Could not download that file. Send a link instead."
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                async with c.stream("GET", f"{TG}/file/bot{self.token}/{path}") as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > FILE_LIMIT:
                            err = "That file is over 20 MB. Send a link instead."
                            raise ValueError("over the Bot API download limit")
                        out.write(chunk)
            return tmp, None
        except Exception as e:
            log.warning("file intake failed: %s", self._safe(e))
            _unlink(tmp)
            return None, err

    async def _from_file(self, c, chat_id, doc, fields, settings):
        tmp, err = await self._tg_download(c, doc)
        if tmp is None:
            return await self._say(c, chat_id, err)
        try:
            with open(tmp, "rb") as f:
                status, body = await self._api(
                    c, "POST", "/api/process", data={"acknowledged": "true", **fields},
                    files={"file": (doc.get("file_name") or "video.mp4", f, "video/mp4")})
        finally:
            _unlink(tmp)
        await self._queued(c, chat_id, status, body, settings)

    # --- split-screen library -----------------------------------------
    async def _library_add(self, c, doc, name):
        """(library item, None) for a Telegram video added through the API, or (None, reason)."""
        tmp, err = await self._tg_download(c, doc)
        if tmp is None:
            return None, err
        ext = os.path.splitext(doc.get("file_name") or tmp)[1].lower() or ".mp4"
        try:
            with open(tmp, "rb") as f:
                status, body = await self._api(
                    c, "POST", "/api/slot-uploads", data={"name": name}, timeout=600,
                    files={"file": (f"telegram{ext}", f, "video/mp4")})
        finally:
            _unlink(tmp)
        if status == 200 and isinstance(body.get("id"), str):
            return body, None
        return None, "OpenShorts is not reachable" if status == 0 else self._detail(body)

    async def _save_video(self, c, chat_id, doc, rest):
        name = preset_name(rest)
        if not name:
            return await self._say(c, chat_id, "Add a name: send the video with the caption /save <name>.")
        item, err = await self._library_add(c, doc, name)
        await self._say(c, chat_id, f"Saved {item['name']} in the library." if item
                        else f"Could not save it: {err}")

    def _waiting_panel(self, chat_id, waiting):
        return self._panels(chat_id).get(waiting.get("panel") or "")

    async def _split_video(self, c, chat_id, waiting, doc):
        if self._waiting_panel(chat_id, waiting) is None:
            return await self._say(c, chat_id, "That panel is gone. Send the link again.")
        if int(doc.get("file_size") or 0) > FILE_LIMIT:
            return await self._say(c, chat_id, "That file is over 20 MB, the most a bot can download. "
                                               "Use Send a link instead.")
        stem = os.path.splitext(doc.get("file_name") or "")[0]
        item, err = await self._library_add(c, doc, preset_name(stem) or "telegram video")
        if item is None:
            return await self._say(c, chat_id, f"Could not use that video: {err}")
        await self._use_src(c, chat_id, waiting["panel"], item)

    async def _split_link(self, c, chat_id, waiting, url):
        if self._waiting_panel(chat_id, waiting) is None:
            return await self._say(c, chat_id, "That panel is gone. Send the link again.")
        await self._say(c, chat_id, "Downloading the extra video on the server...")
        self._spawn(self._split_link_task(chat_id, waiting["panel"], url))

    async def _split_link_task(self, chat_id, mid, url):
        # The bot never fetches the link: the API downloads it under its own guards.
        async with self.client_factory() as c:
            status, body = await self._api(c, "POST", "/api/slot-uploads", data={"url": url},
                                           timeout=1800)
            if status != 200 or not isinstance(body.get("id"), str):
                reason = "OpenShorts is not reachable" if status == 0 else self._detail(body)
                return await self._say(c, chat_id, f"Could not use that link: {reason}")
            await self._use_src(c, chat_id, mid, body)

    async def _use_src(self, c, chat_id, mid, item):
        name = str(item.get("name") or "video")[:64]
        panel = self._panels(chat_id).get(str(mid))
        if panel is None:
            return await self._say(c, chat_id, f"Saved {name} in the library, but that panel is gone.")
        panel["s"]["src"] = {"id": item["id"], "name": name}
        self._save()
        await self._redraw(c, chat_id, int(mid), panel, self.split_keyboard(panel["s"]))
        await self._say(c, chat_id, f"Extra video ready: {name} (saved in /library).")

    async def _library_view(self, c):
        items, err = await self._library_items(c)
        if items is None:
            return f"Could not read the library: {err}", None
        if not items:
            return ("The library is empty. Send a video with the caption /save <name>, or put "
                    "videos in the server's import folder."), None
        lines = [f"{n + 1}. {i.get('name') or 'video'} ({float(i.get('duration') or 0):.0f} s)"
                 for n, i in enumerate(items)]
        return "Library (tap 🗑 to delete):\n" + "\n".join(lines), self.library_keyboard(items)

    async def _library(self, c, chat_id, rest):
        status, body = await self._api(c, "POST", "/api/slot-uploads/import", timeout=600)
        note = ""
        if status == 200:
            if body.get("imported"):
                note += "Imported from the folder: " + ", ".join(map(str, body["imported"])) + "\n"
            if body.get("skipped"):
                note += "Left in the folder (not a usable video): " + ", ".join(map(str, body["skipped"])) + "\n"
        text, markup = await self._library_view(c)
        await self._say(c, chat_id, (note + "\n" if note else "") + text, markup)

    async def _library_delete(self, c, chat_id, mid, item_id):
        if not _ID_RE.match(item_id):
            return None
        status, body = await self._api(c, "DELETE", f"/api/slot-uploads/{item_id}")
        text, markup = await self._library_view(c)
        await self._edit(c, chat_id, mid, text, markup)
        if status == 200:
            return "Deleted."
        return "Already gone." if status == 404 else f"Could not delete: {self._detail(body)}"

    async def _queued(self, c, chat_id, status, body, settings):
        job_id = body.get("job_id") if isinstance(body, dict) else None
        if status != 200 or not job_id:
            reason = "OpenShorts is not reachable" if status == 0 else self._detail(body)
            return await self._say(c, chat_id, f"Could not queue it: {reason}")
        self.data["job_settings"][job_id] = dict(settings)
        self._remember(chat_id, job_id)
        mid = await self._say(c, chat_id, f"Queued job {job_id[:8]}", self.cancel_keyboard(job_id))
        self._spawn(self.track(chat_id, job_id, mid))

    # --- progress and delivery -----------------------------------------
    async def track(self, chat_id, job_id, msg_id, is_active: Callable[[], bool] = lambda: True):
        """Poll the job; edit ONE message per stage change; deliver on completion."""
        interval, last, short = 15.0, "queued", job_id[:8]
        async with self.client_factory() as c:
            for _ in range(2000):
                if not is_active():
                    return
                await self.sleep(interval)
                status, body = await self._api(c, "GET", f"/api/status/{job_id}")
                if status == 404:
                    return await self._edit(c, chat_id, msg_id, f"Job {short} is gone.")
                st = body.get("status") if status == 200 else None
                if st is None:
                    interval = min(60.0, interval * 1.5)
                    continue
                if st != last and st in ("queued", "processing"):
                    last, interval = st, 15.0
                    msg_id = await self._edit(c, chat_id, msg_id, f"Job {short}: {st}",
                                              self.cancel_keyboard(job_id))
                else:
                    interval = min(60.0, interval * 1.5)
                if st == "cancelled":
                    return await self._edit(c, chat_id, msg_id, f"Job {short}: cancelled")
                if st == "completed":
                    clips = (body.get("result") or {}).get("clips") or []
                    await self._edit(c, chat_id, msg_id, f"Job {short}: done, {len(clips)} clips")
                    s = clean_settings(self.data["job_settings"].get(job_id))
                    for i, clip in enumerate(clips):
                        note = None
                        if s["spl"] == "on" and s["src"]:
                            clip, note = await self._apply_split(c, job_id, i, clip, s)
                        await self._deliver(c, chat_id, i, clip, job_id, note)
                    return
                if st == "failed":
                    return await self._edit(c, chat_id, msg_id, self._failure(short, body))

    @staticmethod
    def _failure(short, body) -> str:
        # Self-host logs are raw: only a last line without a path is shown.
        logs = body.get("logs") or []
        last = str(logs[-1]).strip() if logs else ""
        if last and "/" not in last and "\\" not in last:
            return f"Job {short} failed: {last[:300]}"
        return f"Job {short} failed. Open the dashboard for details."

    def _link(self, clip) -> Optional[str]:
        """The signed download_url as served, else public base + the self-host path."""
        dl = clip.get("download_url")
        if isinstance(dl, str) and dl.startswith("http"):
            return dl
        if self.public_url:
            rel = dl if isinstance(dl, str) and dl.startswith("/") else clip.get("video_url")
            if isinstance(rel, str) and rel.startswith("/"):
                return self.public_url + rel
        return None

    async def _apply_split(self, c, job_id, i, clip, s):
        """(clip to deliver, note): the split clip as /api/status now lists it,
        or the plain one with a short note (4xx detail only: a 500 may carry paths)."""
        status, body = await self._api(c, "POST", "/api/clip/split", json=split_body(job_id, i, s),
                                       timeout=1800)
        new = body.get("new_video_url") if status == 200 else None
        if isinstance(new, str) and new.startswith("/videos/"):
            st, sb = await self._api(c, "GET", f"/api/status/{job_id}")
            fresh = ((sb.get("result") or {}).get("clips") or []) if st == 200 else []
            if i < len(fresh) and isinstance(fresh[i], dict) and fresh[i].get("video_url") == new:
                return fresh[i], None   # carries the signed download_url
            return {**clip, "video_url": new, "download_url": None}, None
        reason = (self._detail(body) if 400 <= status < 500 else
                  "OpenShorts is not reachable" if status == 0 else "the render failed")
        return clip, f"Split screen failed ({self._safe(reason)[:150]}), so this is the plain clip."

    async def _deliver(self, c, chat_id, i, clip, job_id=None, note=None):
        title = clip.get("video_title_for_youtube_short") or f"Clip {i + 1}"
        rec = {"chat": chat_id, "i": i, "title": title[:TITLE_CHARS], "link": None, "state": ""}
        if note:
            rec["note"] = note
        markup = self.clip_keyboard(job_id, i, rec) if job_id else None
        mid = None
        if self.send_files:
            mid = await self._send_file(c, chat_id, clip, self.clip_text(rec), markup)
            rec["kind"] = "caption"
        if mid is None:
            rec["link"], rec["kind"] = self._link(clip), "text"
            body = self.clip_text(rec) if rec["link"] else (
                f"{i + 1}. {title}\n(could not send the file; it is in the dashboard)"
                + (f"\n{note}" if note else ""))
            mid = await self._say(c, chat_id, body, markup)
        if job_id and mid is not None:
            rec["msg"] = mid
            self.data["clips"][f"{job_id}:{i}"] = rec
            self._save()

    async def _fetch_clip(self, c, clip):
        """(bytes, file name) of a clip from the loopback API, or None."""
        ref = clip.get("download_url") or clip.get("video_url")
        if not isinstance(ref, str):
            return None
        parts = urlsplit(ref)  # keep path+query (the signature covers them); host is loopback
        url = self.api + parts.path + (f"?{parts.query}" if parts.query else "")
        data = bytearray()
        try:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > SEND_LIMIT:
                        return None
        except Exception as e:
            log.warning("clip download failed: %s", self._safe(e))
            return None
        return bytes(data), os.path.basename(parts.path) or "clip.mp4"

    async def _send_file(self, c, chat_id, clip, caption, markup=None):
        """message_id of the sent video/document, or None."""
        got = await self._fetch_clip(c, clip)
        if got is None:
            return None
        data, name = got
        form = {"chat_id": str(chat_id), "caption": caption[:1000]}
        if markup:
            form["reply_markup"] = json.dumps({"inline_keyboard": markup})
        for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
            r = await self._tg(c, method, data=form, files={field: (name, data, "video/mp4")})
            if r is not None:
                return r.get("message_id") if isinstance(r, dict) else None
        return None

    # --- approval and upload (n8n) -------------------------------------
    def _local(self, when) -> str:
        """A UTC datetime (or its ISO text) as local time with the weekday."""
        if isinstance(when, str):
            when = datetime.fromisoformat(when)
        zone, used = _zone(self.tz)
        return when.astimezone(zone).strftime("%a %d %b %Y %H:%M ") + used

    def clip_text(self, rec) -> str:
        out = f"{rec['i'] + 1}. {rec['title']}"
        if rec.get("note"):
            out += f"\n{rec['note']}"
        if rec.get("link"):
            out += f"\n{rec['link']}"
        if rec.get("late"):
            out += f"\n{rec['late']}"
        st = rec.get("state")
        if st == "scheduled":
            out += f"\n🗓 Scheduled for {self._local(rec['at'])}"
        elif st == "uploading":
            out += "\n⏳ Uploading…"
        elif st == "uploaded":
            out += f"\n✅ On YouTube: {rec.get('youtube_url') or '?'} (Drive: {rec.get('drive_link') or '?'})"
        elif st == "skipped":
            out += "\n⏭ Skipped"
        elif st == "failed":
            out += f"\nUpload failed: {rec.get('error') or 'unknown error'}"
        return out

    def clip_keyboard(self, job_id, i, rec):
        # Without the n8n webhook there is nothing to approve: no buttons at all.
        if not self.n8n_url or rec.get("state") in ("uploading", "uploaded", "skipped"):
            return []
        k = f"{job_id}:{i}"
        if rec.get("state") == "scheduled":
            return [[_btn("❌ Cancel schedule", f"qc:{k}"), _btn("⏫ Upload now", f"u:{k}")],
                    [_btn("✏️ Edit title", f"t:{k}")]]
        return [[_btn("✅ Upload to YouTube", f"u:{k}"), _btn("⏭ Skip", f"k:{k}")],
                [_btn("✏️ Edit title", f"t:{k}"), _btn("🗓 Schedule upload", f"q:{k}")]]

    def schedule_keyboard(self, key, now):
        # callback_data "qa:<code>:<key>"; "today 20:00" only while it is still ahead.
        return [[_btn(label, f"qa:{code}:{key}")] for code, (label, _, _) in QUICK.items()
                if quick_time(code, now, self.tz) > now] + [
            [_btn("⌨️ Type date/time", f"qt:{key}")], [_btn("« Back", f"qb:{key}")]]

    async def _show_clip(self, c, key, rec):
        job_id = key.rsplit(":", 1)[0]
        markup = {"inline_keyboard": self.clip_keyboard(job_id, rec["i"], rec)}
        if rec.get("kind") == "caption":
            await self._tg(c, "editMessageCaption", json={
                "chat_id": rec["chat"], "message_id": rec["msg"],
                "caption": self.clip_text(rec)[:1000], "reply_markup": markup})
        else:
            await self._tg(c, "editMessageText", json={
                "chat_id": rec["chat"], "message_id": rec["msg"],
                "text": self.clip_text(rec)[:4000], "reply_markup": markup})

    async def _clip_action(self, c, chat_id, mid, head, arg):
        # "qa" and "qy" carry "<choice or epoch>:<job id>:<index>", the others the key alone.
        extra, key = arg.partition(":")[::2] if head in ("qa", "qy") else ("", arg)
        rec = self.data["clips"].get(key)
        if rec is None or rec.get("chat") != chat_id:
            return "I do not know that clip any more."
        if rec.get("state") == "uploaded":
            return f"Already uploaded: {rec.get('youtube_url') or ''}".strip()
        if rec.get("state") == "uploading":
            return "Already uploading, wait for it."
        if head.startswith("q"):
            return await self._schedule_action(c, chat_id, mid, head, extra, key, rec)
        if head == "k":
            rec["state"] = "skipped"
            rec.pop("at", None)
        elif head == "t":
            self.data["awaiting"][str(chat_id)] = {"kind": "title", "key": key}
            self._save()
            await self._say(c, chat_id, f"Send the new title for clip {rec['i'] + 1} "
                                        f"(up to {TITLE_CHARS} characters).")
            return None
        else:
            if not self.n8n_url:
                return "Upload is off on this server."
            rec["state"] = "uploading"   # also "⏫ Upload now" on a scheduled clip
            rec.pop("at", None)
            self._spawn(self._upload(key))
        self._save()
        await self._show_clip(c, key, rec)
        return "Uploading…" if head == "u" else "Skipped."

    # --- scheduled YouTube uploads ------------------------------------
    # One state per clip: "" -> scheduled -> uploading -> uploaded | failed. The
    # scheduler sends the same n8n request as the button (_upload), at rec["at"].
    async def _schedule_action(self, c, chat_id, mid, head, extra, key, rec):
        if not self.n8n_url:
            return "Upload is off on this server."
        if head == "q":
            if rec.get("state") == "scheduled":
                return "Already scheduled. Cancel the schedule first."
            await self._keys(c, rec["chat"], rec["msg"], self.schedule_keyboard(key, self.now()))
        elif head == "qb":
            await self._show_clip(c, key, rec)
        elif head == "qt":
            self.data["awaiting"][str(chat_id)] = {"kind": "when", "key": key}
            self._save()
            await self._say(c, chat_id, f"Send the upload time for clip {rec['i'] + 1} as "
                                        f"YYYY-MM-DD HH:MM ({_zone(self.tz)[1]}).")
        elif head == "qa":
            when = quick_time(extra, self.now(), self.tz)
            if when is None:
                return None
            await self._show_clip(c, key, rec)   # the menu goes back to the normal buttons
            return await self._ask_schedule(c, chat_id, key, rec, when)
        elif head == "qn":
            await self._edit(c, chat_id, mid, "Not scheduled.")
            return "Not scheduled."
        elif head == "qy":
            try:
                when = datetime.fromtimestamp(int(extra), timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
            if (err := check_when(when, self.now())):
                await self._edit(c, chat_id, mid, f"Not scheduled: {err}")
                return err
            rec.update(state="scheduled", at=when.isoformat(), keep=True)
            rec.pop("late", None)
            self._save()
            await self._show_clip(c, key, rec)
            # A day of slack past the run: the API deletes job files after 24 h.
            status, _ = await self._api(c, "POST", self._keep_path(key, rec),
                                        json={"until": (when + timedelta(days=1)).isoformat()})
            warn = ("" if status == 200 else "\n⚠️ Could not protect the file from cleanup; "
                                             "it may be deleted after 24 h.")
            await self._edit(c, chat_id, mid,
                             f"🗓 Scheduled clip {rec['i'] + 1} for {self._local(when)}.{warn}")
            return "Scheduled."
        elif head in ("qc", "qx"):
            if rec.get("state") != "scheduled":
                return "That clip is not scheduled."
            rec["state"] = ""
            rec.pop("at", None)
            rec.pop("late", None)
            self._save()
            await self._show_clip(c, key, rec)
            await self._unkeep(c, key, rec)
            if head == "qx":   # pressed in the /scheduled list: redraw it
                text, markup = await self._scheduled_view(c, chat_id)
                await self._edit(c, chat_id, mid, text, markup)
            return "Schedule cancelled."
        return None

    @staticmethod
    def _keep_path(key, rec):
        return f"/api/jobs/{key.rsplit(':', 1)[0]}/clips/{rec['i']}/keep"

    async def _unkeep(self, c, key, rec):
        """Drop the cleanup protection a schedule set (best effort)."""
        if rec.pop("keep", None):
            self._save()
            await self._api(c, "DELETE", self._keep_path(key, rec))

    async def _ask_schedule(self, c, chat_id, key, rec, when):
        """Send the Yes/No confirmation; returns the reason when ``when`` is refused."""
        if (err := check_when(when, self.now())):
            return err
        priv = clean_settings(self.data["job_settings"].get(key.rsplit(":", 1)[0]))["priv"]
        epoch = int(when.timestamp())
        await self._say(c, chat_id, f"Schedule upload for {self._local(when)} (privacy: {priv})?\n"
                                    f"Clip {rec['i'] + 1}: {rec['title']}",
                        [[_btn("✅ Yes", f"qy:{epoch}:{key}"), _btn("✖️ No", f"qn:{key}")]])
        return None

    async def _typed_when(self, c, chat_id, key, text):
        rec = self.data["clips"].get(key or "")
        if rec is None or rec.get("chat") != chat_id:
            return await self._say(c, chat_id, "I do not know that clip any more.")
        again = " Press 🗓 Schedule upload again."
        try:
            when = parse_when(text, self.tz)[0].astimezone(timezone.utc)
        except ValueError:
            return await self._say(c, chat_id, "Use YYYY-MM-DD HH:MM, for example 2030-01-31 18:00." + again)
        if rec.get("state") in ("uploading", "uploaded"):
            return await self._say(c, chat_id, "That clip is already uploaded.")
        if (err := await self._ask_schedule(c, chat_id, key, rec, when)):
            await self._say(c, chat_id, err + again)

    async def run_due(self):
        """Start every scheduled upload whose time has come, including the ones
        missed while the server was down (those say they ran late)."""
        if not self.n8n_url:
            return
        now = self.now()
        due = []
        for key, rec in self.data["clips"].items():
            if rec.get("state") != "scheduled":
                continue
            try:
                at = datetime.fromisoformat(rec.get("at") or "")
                late = now - at > LATE_AFTER
                ready = at <= now
            except (TypeError, ValueError):   # unreadable time: run it rather than keep it forever
                at, late, ready = None, False, True
            if ready:
                # Moved off "scheduled" before any await: no second tick or press can start it.
                rec["state"] = "uploading"
                rec.pop("at", None)
                if late:
                    rec["late"] = f"⏰ Ran late: it was scheduled for {self._local(at)}."
                due.append(key)
        if not due:
            return
        self._save()
        async with self.client_factory() as c:
            for key in due:
                await self._show_clip(c, key, self.data["clips"][key])
                self._spawn(self._upload(key))

    async def _scheduler(self, stop, is_active):
        while not stop.is_set() and is_active():
            try:
                await self.run_due()
            except Exception as e:
                log.warning("scheduler failed: %s", self._safe(e))
            await self.tick_sleep(TICK_SECONDS)

    async def _set_title(self, c, chat_id, key, text):
        rec = self.data["clips"].get(key or "")
        title = " ".join(text.split())[:TITLE_CHARS]
        if rec is None or not title:
            return await self._say(c, chat_id, "No title set.")
        if rec.get("state") in ("uploading", "uploaded"):
            return await self._say(c, chat_id, "That clip is already uploaded; title unchanged.")
        rec["title"] = title
        self._save()
        await self._show_clip(c, key, rec)
        await self._say(c, chat_id, f"Title of clip {rec['i'] + 1} set.")

    async def _upload(self, key):
        """POST one clip to the n8n webhook (contract in docs/telegram-bot.md)."""
        rec = self.data["clips"][key]
        job_id, i = key.rsplit(":", 1)[0], rec["i"]
        settings = clean_settings(self.data["job_settings"].get(job_id))
        result = {"ok": False, "error": "could not read the clip from OpenShorts"}
        async with self.client_factory() as c:
            status, body = await self._api(c, "GET", f"/api/status/{job_id}")
            clips = ((body.get("result") or {}).get("clips") or []) if status == 200 else []
            clip = clips[i] if i < len(clips) else None
            got = await self._fetch_clip(c, clip) if clip else None  # fresh signature
            if got:
                desc = (clip.get("video_description_for_instagram")
                        or clip.get("video_description_for_tiktok") or rec["title"])[:5000]
                tags = ",".join(dict.fromkeys(re.findall(r"#(\w+)", desc)))
                try:
                    r = await c.post(
                        self.n8n_url, timeout=600,
                        headers={"X-OpenShorts-Secret": self.n8n_secret},
                        data={"title": rec["title"][:TITLE_CHARS], "description": desc,
                              "tags": tags, "privacy": settings["priv"],
                              "job_id": job_id, "clip_index": str(i)},
                        files={"video": (got[1], got[0], "video/mp4")})
                    try:
                        result = r.json()
                    except ValueError:
                        result = {"ok": False, "error": f"n8n answered HTTP {r.status_code}"}
                    if not isinstance(result, dict):
                        result = {"ok": False, "error": "n8n answered something unexpected"}
                except Exception as e:
                    log.warning("n8n upload failed: %s", self._safe(e))
                    result = {"ok": False, "error": "n8n did not answer (timeout or network)"}
            if result.get("ok") is True:
                rec.update(state="uploaded", youtube_url=str(result.get("youtube_url") or "")[:300],
                           drive_link=str(result.get("drive_link") or "")[:300])
            else:
                rec.update(state="failed", error=self._safe(result.get("error") or "unknown error")[:200])
            self._save()
            await self._show_clip(c, key, rec)
            await self._unkeep(c, key, rec)   # no automatic retry: the file is free to go

    # --- cancelling jobs ------------------------------------------------
    # callback_data "cj|cy|cn:<where>:<job id>": ask, yes, no. <where> is "s" for
    # the job's status message, "l" for the /jobs list (redrawn after).
    @staticmethod
    def cancel_keyboard(job_id, where="s"):
        return [[_btn("🛑 Cancel job", f"cj:{where}:{job_id}")]]

    @staticmethod
    def confirm_keyboard(job_id, where):
        return [[_btn("Yes, cancel", f"cy:{where}:{job_id}"), _btn("No", f"cn:{where}:{job_id}")]]

    async def _jobs_view(self, c):
        status, body = await self._api(c, "GET", "/api/jobs/active")
        if status != 200:
            reason = "OpenShorts is not reachable" if status == 0 else self._detail(body)
            return f"Could not list jobs: {reason}", None
        rows = [j for j in body.get("jobs") or [] if isinstance(j, dict)
                and _JOB_RE.match(str(j.get("job_id")))]
        if not rows:
            return "No jobs queued or running.", None
        lines = []
        for n, j in enumerate(rows):
            jid, st = j["job_id"], j.get("status")
            if st == "queued":
                what = f"queued, #{j['position']} in line" if j.get("position") else "queued"
            else:
                stage = str(j.get("stage") or "").strip()
                # Self-host logs are raw: a line with a path is not shown (as in _failure).
                what = f"processing: {stage[:120]}" if stage and "/" not in stage and "\\" not in stage \
                    else "processing"
            src = f" {str(j.get('source'))[:80]}" if j.get("source") else ""
            lines.append(f"{n + 1}. {jid[:8]} {what}{src}")
        markup = [[_btn(f"🛑 Cancel {j['job_id'][:8]}", f"cj:l:{j['job_id']}")] for j in rows]
        return "Active jobs:\n" + "\n".join(lines), markup

    async def _jobs(self, c, chat_id, rest):
        text, markup = await self._jobs_view(c)
        await self._say(c, chat_id, text, markup)

    async def _clips(self, c, chat_id, rest):
        """Resend a finished job's clips with live buttons (latest job by default)."""
        short = (rest or "").strip()
        recent = self.jobs.get(str(chat_id), [])
        job_id = self._resolve(chat_id, short) if short else (recent[-1] if recent else None)
        if not job_id:
            return await self._say(c, chat_id, "No such job. Use the short id from its Queued message.")
        status, body = await self._api(c, "GET", f"/api/status/{job_id}")
        done = status == 200 and body.get("status") == "completed"
        clips = ((body.get("result") or {}).get("clips") or []) if done else []
        if not clips:
            return await self._say(c, chat_id, f"Job {job_id[:8]} has no finished clips.")
        for i, clip in enumerate(clips):
            rec = self.data["clips"].get(f"{job_id}:{i}")
            if rec is None:
                await self._deliver(c, chat_id, i, clip, job_id)
                continue
            # Keep the record (edited title, schedule, upload state); only the message is new.
            markup = self.clip_keyboard(job_id, i, rec)
            mid = await self._send_file(c, chat_id, clip, self.clip_text(rec), markup) if self.send_files else None
            rec["kind"] = "caption"
            if mid is None:
                rec["kind"] = "text"
                mid = await self._say(c, chat_id, self.clip_text(rec), markup)
            if mid is not None:
                rec["msg"] = mid
                self._save()

    async def _job_cancel(self, c, chat_id, mid, head, arg):
        where, _, job_id = arg.partition(":")
        if where not in ("s", "l") or not _JOB_RE.match(job_id):
            return None
        if head == "cj":
            await self._keys(c, chat_id, mid, self.confirm_keyboard(job_id, where))
            return "Cancel this job?"
        if head == "cn":
            if where == "l":
                text, markup = await self._jobs_view(c)
                await self._edit(c, chat_id, mid, text, markup)
            else:
                await self._keys(c, chat_id, mid, self.cancel_keyboard(job_id))
            return "Kept."
        status, body = await self._api(c, "POST", f"/api/jobs/{job_id}/cancel")
        if status == 200:
            if where == "l":
                text, markup = await self._jobs_view(c)
                await self._edit(c, chat_id, mid, text, markup)
            else:
                await self._edit(c, chat_id, mid, f"Job {job_id[:8]}: cancelled")
            return "Cancelled."
        if where == "s":
            await self._keys(c, chat_id, mid, [])
        return "OpenShorts is not reachable." if status == 0 else self._detail(body)[:150]

    # --- scheduling -----------------------------------------------------
    def _need_profile(self):
        return None if (self.upload_key and self.profile) else (
            "Scheduling needs UPLOAD_POST_API_KEY and TELEGRAM_UPLOAD_POST_PROFILE on the server.")

    async def _schedule(self, c, chat_id, rest):
        usage = "Usage: /schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM>"
        if (err := self._need_profile()):
            return await self._say(c, chat_id, err)
        parts = rest.split(None, 3)
        if len(parts) != 4:
            return await self._say(c, chat_id, usage)
        short, num, plats, when = parts
        job = self._resolve(chat_id, short)
        platforms = [p.strip().lower() for p in plats.split(",") if p.strip()]
        if not job:
            return await self._say(c, chat_id, "I do not know that job. Use the short id from the Queued message.")
        if not num.isdigit() or int(num) < 1:
            return await self._say(c, chat_id, "Clip number starts at 1.")
        if not platforms or any(p not in PLATFORMS for p in platforms):
            return await self._say(c, chat_id, "Platforms must be from: tiktok, instagram, youtube.")
        try:
            dt, used = parse_when(when, self.tz)
        except ValueError:
            return await self._say(c, chat_id, usage)
        if dt <= self.now():
            return await self._say(c, chat_id, "That time is in the past.")
        status, body = await self._api(
            c, "POST", "/api/social/post",
            headers={"X-Upload-Post-Key": self.upload_key},
            json={"job_id": job, "clip_index": int(num) - 1, "platforms": platforms,
                  "user_id": self.profile, "scheduled_date": dt.isoformat(), "timezone": used})
        if status == 200:
            note = "" if used == self.tz else f" (zone {self.tz} unknown here, used UTC)"
            return await self._say(c, chat_id, f"Scheduled clip {num} for {when} {used}{note}.")
        await self._say(c, chat_id, f"Could not schedule: {self._detail(body)}")

    async def _rows(self, c):
        status, body = await self._api(c, "GET", "/api/social/scheduled",
                                       headers={"X-Upload-Post-Key": self.upload_key},
                                       params={"user": self.profile})
        return status, body, sorted((body.get("scheduled_posts") or []) if status == 200 else [],
                                    key=lambda r: r.get("scheduled_date") or "")

    async def _scheduled_view(self, c, chat_id):
        """(text, markup): this chat's scheduled YouTube uploads, then the
        Upload-Post posts when Upload-Post is configured."""
        mine = sorted(((k, r) for k, r in self.data["clips"].items()
                       if r.get("state") == "scheduled" and r.get("chat") == chat_id),
                      key=lambda kr: kr[1].get("at") or "")
        parts, markup = [], []
        if mine:
            parts.append("YouTube uploads (tap ❌ to cancel):\n" + "\n".join(
                f"{n + 1}. {r['title']} - {self._local(r['at'])}" for n, (_, r) in enumerate(mine)))
            markup = [[_btn(f"❌ {n + 1}. {r['title'][:30]}", f"qx:{k}")] for n, (k, r) in enumerate(mine)]
        if self._need_profile() is None:
            status, body, rows = await self._rows(c)
            if status != 200:
                parts.append(f"Upload-Post: could not list: {self._detail(body)}")
            elif rows:
                parts.append("Upload-Post (id, platform, time):\n" + "\n".join(
                    f"{str(r.get('job_id', ''))[-8:]}  {r.get('platform') or ','.join(r.get('platforms') or [])}  "
                    f"{r.get('scheduled_date', '')}" for r in rows))
        return "\n\n".join(parts) or "Nothing scheduled.", markup or None

    async def _scheduled(self, c, chat_id, rest):
        text, markup = await self._scheduled_view(c, chat_id)
        await self._say(c, chat_id, text, markup)

    async def _cancel(self, c, chat_id, rest):
        if (err := self._need_profile()):
            return await self._say(c, chat_id, err)
        if not rest:
            return await self._say(c, chat_id, "Usage: /cancel <id> (see /scheduled)")
        status, body, rows = await self._rows(c)
        match = [r for r in rows if str(r.get("job_id", "")).endswith(rest.strip())]
        if len(match) != 1:
            return await self._say(c, chat_id, "No scheduled post with that id." if not match
                                   else "That id is ambiguous, send more of it.")
        status, body = await self._api(c, "DELETE", f"/api/social/scheduled/{match[0]['job_id']}",
                                       headers={"X-Upload-Post-Key": self.upload_key},
                                       params={"user": self.profile})
        await self._say(c, chat_id, "Cancelled." if status == 200
                        else f"Could not cancel: {self._detail(body)}")

    # --- polling loop ---------------------------------------------------
    async def run(self, stop: asyncio.Event, is_active: Callable[[], bool] = lambda: True):
        """Never raises. Stops when `stop` is set or the instance drains."""
        delay, conflict_logged = 1.0, False
        self._spawn(self._scheduler(stop, is_active))   # first check now: runs uploads missed while down
        try:
            async with self.client_factory() as c:
                while not stop.is_set() and is_active():
                    try:
                        r = await c.post(f"{TG}/bot{self.token}/getUpdates", timeout=65,
                                         json={"offset": self.offset, "timeout": 50,
                                               "allowed_updates": ["message", "callback_query"]})
                        if r.status_code == 409:
                            if not conflict_logged:
                                log.warning("another poller uses this bot token; backing off")
                                conflict_logged = True
                            await self.sleep(30)
                            continue
                        if r.status_code != 200:
                            raise RuntimeError(f"getUpdates HTTP {r.status_code}")
                        conflict_logged = False
                        updates = r.json().get("result") or []
                        delay = 1.0
                        for u in updates:
                            self.offset = max(self.offset, int(u["update_id"]) + 1)
                            self._save()
                            await self.handle_update(u)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.warning("polling error: %s", self._safe(e))
                        await self.sleep(delay + random.random())
                        delay = min(60.0, delay * 2)
        finally:
            for t in list(self._tasks):
                t.cancel()


async def run_from_env(is_active: Callable[[], bool] = lambda: True, env=None):
    """Entry point used by app.lifespan (and `python -m telegram_bot`)."""
    env = os.environ if env is None else env
    # httpx logs every request URL at INFO, and Bot API URLs carry the token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    allowed = _parse_ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    if not allowed:
        log.warning("intake bot disabled: no allowed chat ids")
        return
    base = env.get("TELEGRAM_API_BASE") or f"http://127.0.0.1:{env.get('PORT', '8000')}"
    bot = IntakeBot(
        base, env["TELEGRAM_INTAKE_TOKEN"], allowed,
        lambda: httpx.AsyncClient(timeout=httpx.Timeout(120.0)),
        env.get("TELEGRAM_STATE_PATH") or os.path.join("output", ".telegram_state.json"),
        tz=env.get("TELEGRAM_TIMEZONE") or "UTC",
        upload_key=env.get("UPLOAD_POST_API_KEY", ""),
        profile=env.get("TELEGRAM_UPLOAD_POST_PROFILE", ""),
        send_files=env.get("TELEGRAM_SEND_FILES", "1") != "0",
        public_url=env.get("PUBLIC_API_URL", ""),
        n8n_url=env.get("N8N_UPLOAD_WEBHOOK_URL", ""),
        n8n_secret=env.get("N8N_WEBHOOK_SECRET", ""))
    if bot.n8n_url and not bot.n8n_secret:
        # Fail closed: never post clips to the webhook without its secret.
        log.warning("YouTube upload disabled: N8N_UPLOAD_WEBHOOK_URL is set without N8N_WEBHOOK_SECRET")
        bot.n8n_url = ""
    log.info("intake bot started for %d chat(s)", len(allowed))
    await bot.run(asyncio.Event(), is_active)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_from_env())
