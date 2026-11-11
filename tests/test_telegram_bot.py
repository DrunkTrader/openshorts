"""Intake bot: httpx.MockTransport only, no network, no real token."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import telegram_bot as tb

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"
CHAT = 42
API = "http://127.0.0.1:9"


class Rig:
    """Records every request; `routes` maps 'METHOD path-or-tg-method' to a handler."""

    def __init__(self, tmp_path, **kw):
        self.calls, self.routes = [], {}
        self.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        kw.setdefault("send_files", False)
        self.bot = tb.IntakeBot(API, TOKEN, {CHAT}, self.client,
                                str(tmp_path / "state.json"), **kw)
        self.bot.sleep = self._nosleep

    async def _nosleep(self, s):
        return None

    def _handle(self, req: httpx.Request):
        self.calls.append(req)
        if "api.telegram.org" in req.url.host:
            key = req.url.path.split("/")[-1]
        else:
            key = f"{req.method} {req.url.path}"
        h = self.routes.get(key)
        if h is None:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        return h(req) if callable(h) else h

    def named(self, key):
        return [c for c in self.calls if c.url.path.endswith("/" + key) or f"{c.method} {c.url.path}" == key]

    def texts(self):
        return [json.loads(c.content)["text"] for c in self.named("sendMessage")]


def msg(text=None, chat=CHAT, **extra):
    m = {"chat": {"id": chat}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def run(coro):
    return asyncio.run(coro)


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_disabled_without_token_or_allowlist():
    base = {"TELEGRAM_INTAKE_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": "1"}
    assert tb.enabled(base, False)
    assert not tb.enabled({**base, "TELEGRAM_INTAKE_TOKEN": ""}, False)
    assert not tb.enabled({**base, "TELEGRAM_ALLOWED_CHAT_IDS": " , "}, False)
    assert not tb.enabled({"TELEGRAM_INTAKE_TOKEN": "t"}, False)
    assert not tb.enabled(base, True)


def test_unlisted_chat_gets_no_reply_and_no_api_call(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc", chat=999)))
    run(rig.bot.handle_update(msg("/help", chat=999)))
    assert rig.calls == []
    assert rig.bot.ignored == 2


def test_url_message_queues_job_via_api_process(rig):
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": "abcdef12-3456", "status": "queued"})

    async def go():
        await rig.bot.handle_update(msg("watch this https://youtu.be/abc, please"))
        for t in list(rig.bot._tasks):
            t.cancel()
    run(go())
    body = json.loads(rig.named("POST /api/process")[0].content)
    assert body == {"url": "https://youtu.be/abc", "acknowledged": True}
    assert "Queued job abcdef12" in rig.texts()[0]
    assert rig.bot.jobs[str(CHAT)] == ["abcdef12-3456"]


def test_api_refusal_is_reported(rig):
    rig.routes["POST /api/process"] = httpx.Response(400, json={"detail": "URL not allowed"})
    run(rig.bot.handle_update(msg("https://10.0.0.1/x")))
    assert "URL not allowed" in rig.texts()[0]


def test_non_url_text_gets_help_not_job(rig):
    run(rig.bot.handle_update(msg("hello there")))
    assert rig.named("POST /api/process") == []
    assert "/schedule" in rig.texts()[0]


def test_large_file_rejected_with_message(rig):
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 21 * 1024 * 1024})))
    assert rig.named("getFile") == []
    assert "link" in rig.texts()[0]


def test_video_file_posts_multipart(rig, monkeypatch):
    made = []
    real = tb.tempfile.mkstemp

    def spy(*a, **k):
        fd, p = real(*a, **k)
        made.append(p)
        return fd, p
    monkeypatch.setattr(tb.tempfile, "mkstemp", spy)
    rig.routes["getFile"] = ok({"file_path": "videos/f.mp4"})
    rig.routes["GET /file/bot" + TOKEN + "/videos/f.mp4"] = httpx.Response(200, content=b"x" * 100)
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": "j" * 12})

    async def go():
        await rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 100, "file_name": "a.mp4"}))
        for t in list(rig.bot._tasks):
            t.cancel()
    run(go())
    req = rig.named("POST /api/process")[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in req.content and b"acknowledged" in req.content
    assert made and not os.path.exists(made[0])


def status(st, clips=None, logs=None):
    body = {"status": st, "logs": logs or [], "result": {"clips": clips} if clips is not None else None}
    return httpx.Response(200, json=body)


def test_progress_edits_single_message_and_sends_clips_on_completion(tmp_path):
    rig = Rig(tmp_path, public_url="https://pub.example")
    seq = iter([status("queued"), status("queued"), status("processing"), status("processing"),
                status("completed", [
                    {"video_title_for_youtube_short": "A", "video_url": "/videos/j/a.mp4",
                     "download_url": "https://pub.example/videos/j/a.mp4?exp=1&sig=zz"},
                    {"video_title_for_youtube_short": "B", "video_url": "/videos/j/b.mp4"}])])
    rig.routes["GET /api/status/jobjobjob"] = lambda r: next(seq)
    run(rig.bot.track(CHAT, "jobjobjob", 7))
    edits = [json.loads(c.content)["text"] for c in rig.named("editMessageText")]
    assert len(edits) == 2 and "processing" in edits[0] and "2 clips" in edits[1]
    assert all(json.loads(c.content)["message_id"] == 7 for c in rig.named("editMessageText"))
    sent = rig.texts()
    assert "https://pub.example/videos/j/a.mp4?exp=1&sig=zz" in sent[0]
    assert "https://pub.example/videos/j/b.mp4" in sent[1]


def test_failed_job_reports_error_without_logs(rig):
    rig.routes["GET /api/status/jj"] = status(
        "failed", logs=["secret /srv/output/jj/x.log traceback", "Process failed with exit code 1"])
    run(rig.bot.track(CHAT, "jj", 7))
    t = json.loads(rig.named("editMessageText")[0].content)["text"]
    assert "exit code 1" in t and "/srv" not in t and "traceback" not in t
    rig.calls.clear()
    rig.routes["GET /api/status/jj"] = status("failed", logs=["boom at /srv/x"])
    run(rig.bot.track(CHAT, "jj", 7))
    t = json.loads(rig.named("editMessageText")[0].content)["text"]
    assert "/srv" not in t


def test_clip_files_are_sent_from_loopback_with_signature(tmp_path):
    rig = Rig(tmp_path, send_files=True)
    rig.routes["GET /videos/j/a.mp4"] = httpx.Response(200, content=b"v" * 10)
    clip = {"video_url": "/videos/j/a.mp4", "download_url": "https://pub.example/videos/j/a.mp4?exp=1&sig=zz"}
    run(_deliver(rig, clip))
    get = rig.named("GET /videos/j/a.mp4")[0]
    assert str(get.url) == API + "/videos/j/a.mp4?exp=1&sig=zz"
    assert len(rig.named("sendVideo")) == 1 and rig.named("sendDocument") == []


def test_video_falls_back_to_document(tmp_path):
    rig = Rig(tmp_path, send_files=True)
    rig.routes["GET /videos/j/a.mp4"] = httpx.Response(200, content=b"v")
    rig.routes["sendVideo"] = httpx.Response(200, json={"ok": False, "description": "no"})
    run(_deliver(rig, {"video_url": "/videos/j/a.mp4"}))
    assert len(rig.named("sendDocument")) == 1


async def _deliver(rig, clip):
    async with rig.client() as c:
        await rig.bot._deliver(c, CHAT, 0, clip)


def test_media_links_use_signed_download_url_when_secret_configured(tmp_path):
    rig = Rig(tmp_path, public_url="https://pub.example")
    signed = "https://pub.example/videos/j/a.mp4?exp=9&sig=abc"
    run(_deliver(rig, {"video_url": "/videos/j/a.mp4", "download_url": signed}))
    assert rig.texts() == [f"1. Clip 1\n{signed}"]
    # No public URL: a relative signed path is never turned into a link.
    rig2 = Rig(tmp_path)
    run(_deliver(rig2, {"video_url": "/videos/j/a.mp4", "download_url": "/videos/j/a.mp4?exp=9&sig=abc"}))
    assert "videos" not in rig2.texts()[0]


def sched_rig(tmp_path, **kw):
    r = Rig(tmp_path, tz="Europe/Paris", upload_key="K", profile="prof", **kw)
    r.bot.jobs[str(CHAT)] = ["abcdef12-long"]
    return r


def future():
    return (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")


def test_schedule_posts_iso_with_timezone_and_profile(tmp_path):
    r = sched_rig(tmp_path)
    r.routes["POST /api/social/post"] = httpx.Response(200, json={"success": True})
    run(r.bot.handle_update(msg(f"/schedule abcdef12 2 tiktok,youtube {future()}")))
    req = r.named("POST /api/social/post")[0]
    body = json.loads(req.content)
    assert req.headers["X-Upload-Post-Key"] == "K"
    assert body["job_id"] == "abcdef12-long" and body["clip_index"] == 1
    assert body["platforms"] == ["tiktok", "youtube"] and body["user_id"] == "prof"
    assert body["timezone"] == "Europe/Paris"
    assert datetime.fromisoformat(body["scheduled_date"]).utcoffset() in (timedelta(hours=1), timedelta(hours=2))
    assert "Scheduled" in r.texts()[0]


def test_schedule_rejects_past_and_bad_platform(tmp_path):
    r = sched_rig(tmp_path)
    run(r.bot.handle_update(msg("/schedule abcdef12 1 tiktok 2001-01-01 10:00")))
    run(r.bot.handle_update(msg(f"/schedule abcdef12 1 myspace {future()}")))
    run(r.bot.handle_update(msg(f"/schedule nope 1 tiktok {future()}")))
    assert r.named("POST /api/social/post") == []
    assert "past" in r.texts()[0] and "Platforms" in r.texts()[1] and "know" in r.texts()[2]


def test_unknown_timezone_falls_back_to_utc():
    dt, used = tb.parse_when("2030-01-01 10:00", "Not/AZone")
    assert used == "UTC" and dt.utcoffset() == timedelta(0)


def test_scheduled_lists_sorted(tmp_path):
    r = sched_rig(tmp_path)
    r.routes["GET /api/social/scheduled"] = httpx.Response(200, json={"scheduled_posts": [
        {"job_id": "zzzz2222", "platform": "youtube", "scheduled_date": "2030-02-01T10:00:00"},
        {"job_id": "aaaa1111", "platform": "tiktok", "scheduled_date": "2030-01-01T10:00:00"}]})
    run(r.bot.handle_update(msg("/scheduled")))
    out = r.texts()[0]
    assert out.index("aaaa1111") < out.index("zzzz2222")
    assert r.named("GET /api/social/scheduled")[0].url.params["user"] == "prof"


def test_cancel_calls_delete_with_user_param(tmp_path):
    r = sched_rig(tmp_path)
    r.routes["GET /api/social/scheduled"] = httpx.Response(200, json={"scheduled_posts": [
        {"job_id": "xxxxaaaa1111", "platform": "tiktok", "scheduled_date": "2030-01-01"}]})
    r.routes["DELETE /api/social/scheduled/xxxxaaaa1111"] = httpx.Response(200, json={"success": True})
    run(r.bot.handle_update(msg("/cancel aaaa1111")))
    d = r.named("DELETE /api/social/scheduled/xxxxaaaa1111")[0]
    assert d.url.params["user"] == "prof"
    assert r.texts()[0] == "Cancelled."


def test_cancel_unknown_id_is_reported(tmp_path):
    r = sched_rig(tmp_path)
    r.routes["GET /api/social/scheduled"] = httpx.Response(200, json={"scheduled_posts": []})
    run(r.bot.handle_update(msg("/cancel nothing")))
    assert "No scheduled post" in r.texts()[0]
    assert not [c for c in r.calls if c.method == "DELETE"]


def _poll_rig(tmp_path, responses):
    rig = Rig(tmp_path)
    seq = iter(responses)
    stop = asyncio.Event()

    def h(req):
        try:
            return next(seq)
        except StopIteration:
            stop.set()
            return ok([])
    rig.routes["getUpdates"] = h
    return rig, stop


def test_update_offset_persists_and_skips_old_updates(tmp_path):
    rig, stop = _poll_rig(tmp_path, [ok([{"update_id": 10, "message": {"chat": {"id": 999}}}])])
    run(rig.bot.run(stop))
    assert rig.bot.offset == 11
    rig2 = Rig(tmp_path)  # same state file: a restart
    assert rig2.bot.offset == 11
    rig2.routes["getUpdates"] = lambda r: (stop2.set(), ok([]))[1]
    stop2 = asyncio.Event()
    run(rig2.bot.run(stop2))
    assert json.loads(rig2.named("getUpdates")[0].content)["offset"] == 11


def test_409_getupdates_backs_off_once(tmp_path, caplog):
    rig, stop = _poll_rig(tmp_path, [httpx.Response(409, json={}), httpx.Response(409, json={})])
    slept = []

    async def sl(s):
        slept.append(s)
    rig.bot.sleep = sl
    with caplog.at_level(logging.INFO, logger="telegram_bot"):
        run(rig.bot.run(stop))
    assert slept[:2] == [30, 30]
    assert len([r for r in caplog.records if "another poller" in r.getMessage()]) == 1


def test_token_never_in_logs_or_errors(tmp_path, caplog, capsys):
    rig = Rig(tmp_path)

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")
    rig.routes["getUpdates"] = boom
    rig.routes["sendMessage"] = boom
    stop = asyncio.Event()

    async def sl(s):
        stop.set()
    rig.bot.sleep = sl
    with caplog.at_level(logging.DEBUG):
        run(rig.bot.run(stop))
        run(rig.bot.handle_update(msg("/help")))
    out = capsys.readouterr()
    text = caplog.text + out.out + out.err
    assert "polling error" in text and "telegram sendMessage failed" in text
    assert TOKEN not in text and TOKEN.split(":")[1] not in text
