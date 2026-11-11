"""Telegram intake bot (self-host): send a link or a video, get clips back.

Long polling over httpx, no bot framework. It talks to OpenShorts only through
the loopback HTTP API, so every API guard (SSRF check, acknowledgement, rate
limits, signed media URLs, owner checks) stays in force. Separate from the
admin-alert bot in cloud/alerts.py: its own token, never shared.
See docs/telegram-bot.md.
"""
import asyncio
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

log = logging.getLogger("telegram_bot")

TG = "https://api.telegram.org"
FILE_LIMIT = 20 * 1024 * 1024    # Bot API: getFile downloads
SEND_LIMIT = 50 * 1024 * 1024    # Bot API: uploads
PLATFORMS = ("tiktok", "instagram", "youtube")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

HELP = (
    "Send a YouTube (or other video) link and I will queue it and send the clips back here.\n"
    "You can also send a video file up to 20 MB; for bigger videos send a link.\n\n"
    "/schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM> - schedule a clip "
    "(platforms: tiktok,instagram,youtube; job is the short id from Queued)\n"
    "/scheduled - list scheduled posts\n"
    "/cancel <id> - cancel a scheduled post\n"
    "/help - this text"
)


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
                 tz="UTC", upload_key="", profile="", send_files=True, public_url=""):
        self.api = api_base.rstrip("/")
        self.token = token
        self.allowed = set(allowed_chat_ids)
        self.client_factory = client_factory
        self.state_path = state_path
        self.tz, self.upload_key, self.profile = tz, upload_key, profile
        self.send_files, self.public_url = send_files, public_url.rstrip("/")
        self.sleep = asyncio.sleep
        self.offset = 0
        self.jobs: dict = {}      # str(chat_id) -> last 20 job ids
        self.ignored = 0
        self._tasks: set = set()
        self._load()

    # --- plumbing -------------------------------------------------------
    def _safe(self, e) -> str:
        return str(e).replace(self.token, "***")

    def _load(self):
        try:
            import json
            with open(self.state_path) as f:
                s = json.load(f)
            self.offset = int(s.get("offset", 0))
            self.jobs = {str(k): list(v) for k, v in s.get("jobs", {}).items()}
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            jsonfile.write_json_atomic(self.state_path, {"offset": self.offset, "jobs": self.jobs})
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

    async def _say(self, c, chat_id, text):
        r = await self._tg(c, "sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
        return r.get("message_id") if isinstance(r, dict) else None

    async def _edit(self, c, chat_id, msg_id, text):
        if msg_id is None:
            return await self._say(c, chat_id, text)
        await self._tg(c, "editMessageText",
                       json={"chat_id": chat_id, "message_id": msg_id, "text": text[:4000]})
        return msg_id

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

    async def _dispatch(self, c, chat_id, msg):
        doc = msg.get("video") or msg.get("document")
        if doc and (msg.get("video") or str(doc.get("mime_type", "")).startswith("video/")):
            return await self._from_file(c, chat_id, doc)
        text = msg.get("text") or ""
        cmd = parse_command(text)
        if cmd:
            name, rest = cmd
            fn = {"/schedule": self._schedule, "/scheduled": self._scheduled,
                  "/cancel": self._cancel}.get(name)
            if fn:
                return await fn(c, chat_id, rest)
            return await self._say(c, chat_id, HELP)
        url = extract_url(text)
        if not url:
            return await self._say(c, chat_id, HELP)
        status, body = await self._api(c, "POST", "/api/process",
                                       json={"url": url, "acknowledged": True})
        await self._queued(c, chat_id, status, body)

    async def _from_file(self, c, chat_id, doc):
        if int(doc.get("file_size") or 0) > FILE_LIMIT:
            return await self._say(c, chat_id, "That file is over 20 MB, which is the most a bot can "
                                               "download. Send a link instead.")
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
                    c, "POST", "/api/process", data={"acknowledged": "true"},
                    files={"file": (doc.get("file_name") or "video.mp4", f, "video/mp4")})
        except Exception as e:
            log.warning("file intake failed: %s", self._safe(e))
            return await self._say(c, chat_id, "Could not download that file. Send a link instead.")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        await self._queued(c, chat_id, status, body)

    async def _queued(self, c, chat_id, status, body):
        job_id = body.get("job_id") if isinstance(body, dict) else None
        if status != 200 or not job_id:
            reason = "OpenShorts is not reachable" if status == 0 else self._detail(body)
            return await self._say(c, chat_id, f"Could not queue it: {reason}")
        self._remember(chat_id, job_id)
        mid = await self._say(c, chat_id, f"Queued job {job_id[:8]}")
        t = asyncio.create_task(self.track(chat_id, job_id, mid))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

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
                        await self._deliver(c, chat_id, i, clip)
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

    async def _deliver(self, c, chat_id, i, clip):
        title = clip.get("video_title_for_youtube_short") or f"Clip {i + 1}"
        caption = f"{i + 1}. {title}"
        if self.send_files:
            sent = await self._send_file(c, chat_id, clip, caption)
            if sent:
                return
        link = self._link(clip)
        await self._say(c, chat_id, f"{caption}\n{link}" if link else
                        f"{caption}\n(could not send the file; it is in the dashboard)")

    async def _send_file(self, c, chat_id, clip, caption) -> bool:
        ref = clip.get("download_url") or clip.get("video_url")
        if not isinstance(ref, str):
            return False
        parts = urlsplit(ref)  # keep path+query (the signature covers them); host is loopback
        url = self.api + parts.path + (f"?{parts.query}" if parts.query else "")
        data = bytearray()
        try:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return False
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > SEND_LIMIT:
                        return False
        except Exception as e:
            log.warning("clip download failed: %s", self._safe(e))
            return False
        name = os.path.basename(parts.path) or "clip.mp4"
        for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
            r = await self._tg(c, method, data={"chat_id": str(chat_id), "caption": caption[:1000]},
                               files={field: (name, bytes(data), "video/mp4")})
            if r is not None:
                return True
        return False

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
                                               "allowed_updates": ["message"]})
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
        public_url=env.get("PUBLIC_API_URL", ""))
    log.info("intake bot started for %d chat(s)", len(allowed))
    await bot.run(asyncio.Event(), is_active)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_from_env())
