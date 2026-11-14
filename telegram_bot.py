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
from datetime import datetime, timezone
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
    "/presets - list saved presets\n"
    "/preset_delete <name> - delete a preset\n"
    "/preset_default <name> - the preset new panels start from\n"
    "/schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM> - schedule a clip "
    "(platforms: tiktok,instagram,youtube; job is the short id from Queued)\n"
    "/scheduled - list scheduled posts\n"
    "/cancel <id> - cancel a scheduled post\n"
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
DEFAULTS = {"cap": "default", "lay": "auto", "fmt": "vertical", "hook": "pill",
            "n": "auto", "len": "30-60", "priv": "private"}
NAME_BYTES = 32   # preset names ride in callback_data ("l:<name>", 64-byte cap)
TITLE_CHARS = 100


def clean_settings(s) -> dict:
    """Every key present with a value the current code offers (old presets may name
    a style that is gone)."""
    s = s if isinstance(s, dict) else {}
    return {k: s.get(k) if s.get(k) in choices else DEFAULTS[k] for k, (_, choices) in OPTIONS.items()}


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


def parse_when(text: str, tz: str = "UTC"):
    """(aware datetime, tz name actually used). Falls back to UTC when the zone
    database has no such zone (Windows without tzdata). ValueError on bad text."""
    from zoneinfo import ZoneInfo
    naive = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    try:
        zone, used = ZoneInfo(tz), tz
    except Exception:
        zone, used = timezone.utc, "UTC"
    return naive.replace(tzinfo=zone), used


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
        if chat_id not in self.allowed:
            self.ignored += 1
            log.info("ignored update from unlisted chat (%d so far)", self.ignored)
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
        # Any message ends a pending prompt; plain text (not a command or a link) answers it.
        waiting = self.data["awaiting"].pop(str(chat_id), None)
        if waiting:
            self._save()
            if text and not cmd and not extract_url(text):
                return await self._answer_prompt(c, chat_id, waiting, text)
        doc = msg.get("video") or msg.get("document")
        if doc and (msg.get("video") or str(doc.get("mime_type", "")).startswith("video/")):
            if int(doc.get("file_size") or 0) > FILE_LIMIT:
                return await self._say(c, chat_id, "That file is over 20 MB, which is the most a bot can "
                                                   "download. Send a link instead.")
            return await self._open_panel(c, chat_id, {"file": {
                k: doc.get(k) for k in ("file_id", "file_name", "file_size")}})
        if cmd:
            name, rest = cmd
            fn = {"/schedule": self._schedule, "/scheduled": self._scheduled,
                  "/cancel": self._cancel, "/presets": self._presets,
                  "/preset_delete": self._preset_delete,
                  "/preset_default": self._preset_default}.get(name)
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
        lines = [f"{label}: {_label(panel['s'][k])}" for k, (label, _) in OPTIONS.items()]
        return f"New job: {what}\n\n" + "\n".join(lines) + "\n\nPick options, then press Start."

    @staticmethod
    def panel_keyboard(s):
        opts = [_btn(f"{label}: {_label(s[k])}", f"m:{k}") for k, (label, _) in OPTIONS.items()]
        return _rows(opts, 2) + [[_btn("▶️ Start", "go"), _btn("💾 Save as preset", "sv")],
                                 [_btn("📂 Load preset", "ld"), _btn("✖️ Cancel", "x")]]

    @staticmethod
    def option_keyboard(s, key):
        choices = [_btn(("✓ " if v == s[key] else "") + _label(v), f"s:{key}:{v}")
                   for v in OPTIONS[key][1]]
        return _rows(choices, 3) + [[_btn("« Back", "m:")]]

    def presets_keyboard(self, chat_id):
        names = sorted(self._presets_of(chat_id))
        return _rows([_btn(n, f"l:{n}") for n in names], 2) + [[_btn("« Back", "m:")]]

    async def _open_panel(self, c, chat_id, src):
        panel = {"src": src, "s": self._start_settings(chat_id)}
        mid = await self._say(c, chat_id, self.panel_text(panel), self.panel_keyboard(panel["s"]))
        if mid is None:
            return
        panels = self._panels(chat_id)
        panels[str(mid)] = panel
        for old in list(panels)[:-10]:
            del panels[old]
        self._save()

    async def _redraw(self, c, chat_id, mid, panel):
        await self._edit(c, chat_id, mid, self.panel_text(panel), self.panel_keyboard(panel["s"]))

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
        if head in ("u", "k", "t"):
            return await self._clip_action(c, chat_id, head, arg)
        if head == "ow":
            return await self._confirm_overwrite(c, chat_id, mid, arg == "y")
        panel = self._panels(chat_id).get(str(mid))
        if panel is None:
            return "This panel has expired. Send the link again."
        if head == "m":
            await self._keys(c, chat_id, mid, self.option_keyboard(panel["s"], arg)
                             if arg in OPTIONS else self.panel_keyboard(panel["s"]))
        elif head == "s":
            key, _, val = arg.partition(":")
            if key in OPTIONS and val in OPTIONS[key][1]:
                panel["s"][key] = val
                self._save()
                await self._redraw(c, chat_id, mid, panel)
        elif head == "ld":
            if not self._presets_of(chat_id):
                return "No presets yet. Use Save as preset first."
            await self._keys(c, chat_id, mid, self.presets_keyboard(chat_id))
        elif head == "l":
            saved = self._presets_of(chat_id).get(arg)
            if saved is None:
                return "That preset is gone."
            panel["s"] = clean_settings(saved)
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
        panel = self._panels(chat_id).get(waiting.get("panel") or "")
        if panel is None:
            return await self._say(c, chat_id, "That panel is gone; nothing saved.")
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
    async def _from_file(self, c, chat_id, doc, fields, settings):
        info = await self._tg(c, "getFile", json={"file_id": doc.get("file_id")})
        path = (info or {}).get("file_path")
        if not path:
            return await self._say(c, chat_id, "Could not fetch that file from Telegram. Send a link instead.")
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".mp4")
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                async with c.stream("GET", f"{TG}/file/bot{self.token}/{path}") as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > FILE_LIMIT:
                            return await self._say(c, chat_id, "That file is over 20 MB. Send a link instead.")
                        out.write(chunk)
            with open(tmp, "rb") as f:
                status, body = await self._api(
                    c, "POST", "/api/process", data={"acknowledged": "true", **fields},
                    files={"file": (doc.get("file_name") or "video.mp4", f, "video/mp4")})
        except Exception as e:
            log.warning("file intake failed: %s", self._safe(e))
            return await self._say(c, chat_id, "Could not download that file. Send a link instead.")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        await self._queued(c, chat_id, status, body, settings)

    async def _queued(self, c, chat_id, status, body, settings):
        job_id = body.get("job_id") if isinstance(body, dict) else None
        if status != 200 or not job_id:
            reason = "OpenShorts is not reachable" if status == 0 else self._detail(body)
            return await self._say(c, chat_id, f"Could not queue it: {reason}")
        self.data["job_settings"][job_id] = dict(settings)
        self._remember(chat_id, job_id)
        mid = await self._say(c, chat_id, f"Queued job {job_id[:8]}")
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
                    msg_id = await self._edit(c, chat_id, msg_id, f"Job {short}: {st}")
                else:
                    interval = min(60.0, interval * 1.5)
                if st == "completed":
                    clips = (body.get("result") or {}).get("clips") or []
                    await self._edit(c, chat_id, msg_id, f"Job {short}: done, {len(clips)} clips")
                    for i, clip in enumerate(clips):
                        await self._deliver(c, chat_id, i, clip, job_id)
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

    async def _deliver(self, c, chat_id, i, clip, job_id=None):
        title = clip.get("video_title_for_youtube_short") or f"Clip {i + 1}"
        rec = {"chat": chat_id, "i": i, "title": title[:TITLE_CHARS], "link": None, "state": ""}
        markup = self.clip_keyboard(job_id, i, rec) if job_id else None
        mid = None
        if self.send_files:
            mid = await self._send_file(c, chat_id, clip, self.clip_text(rec), markup)
            rec["kind"] = "caption"
        if mid is None:
            rec["link"], rec["kind"] = self._link(clip), "text"
            body = self.clip_text(rec) if rec["link"] else (
                f"{i + 1}. {title}\n(could not send the file; it is in the dashboard)")
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
    @staticmethod
    def clip_text(rec) -> str:
        out = f"{rec['i'] + 1}. {rec['title']}"
        if rec.get("link"):
            out += f"\n{rec['link']}"
        st = rec.get("state")
        if st == "uploading":
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
        return [[_btn("✅ Upload to YouTube", f"u:{k}"), _btn("⏭ Skip", f"k:{k}")],
                [_btn("✏️ Edit title", f"t:{k}")]]

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

    async def _clip_action(self, c, chat_id, head, key):
        rec = self.data["clips"].get(key)
        if rec is None or rec.get("chat") != chat_id:
            return "I do not know that clip any more."
        if rec.get("state") == "uploaded":
            return f"Already uploaded: {rec.get('youtube_url') or ''}".strip()
        if rec.get("state") == "uploading":
            return "Already uploading, wait for it."
        if head == "k":
            rec["state"] = "skipped"
        elif head == "t":
            self.data["awaiting"][str(chat_id)] = {"kind": "title", "key": key}
            self._save()
            await self._say(c, chat_id, f"Send the new title for clip {rec['i'] + 1} "
                                        f"(up to {TITLE_CHARS} characters).")
            return None
        else:
            if not self.n8n_url:
                return "Upload is off on this server."
            rec["state"] = "uploading"
            self._spawn(self._upload(key))
        self._save()
        await self._show_clip(c, key, rec)
        return "Uploading…" if head == "u" else "Skipped."

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
        if dt <= datetime.now(timezone.utc):
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

    async def _scheduled(self, c, chat_id, rest):
        if (err := self._need_profile()):
            return await self._say(c, chat_id, err)
        status, body, rows = await self._rows(c)
        if status != 200:
            return await self._say(c, chat_id, f"Could not list: {self._detail(body)}")
        if not rows:
            return await self._say(c, chat_id, "Nothing scheduled.")
        lines = [f"{str(r.get('job_id', ''))[-8:]}  {r.get('platform') or ','.join(r.get('platforms') or [])}  "
                 f"{r.get('scheduled_date', '')}" for r in rows]
        await self._say(c, chat_id, "Scheduled (id, platform, time):\n" + "\n".join(lines))

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
        log.warning("N8N_UPLOAD_WEBHOOK_URL is set without N8N_WEBHOOK_SECRET")
    log.info("intake bot started for %d chat(s)", len(allowed))
    await bot.run(asyncio.Event(), is_active)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_from_env())
