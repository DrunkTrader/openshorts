"""Intake bot: httpx.MockTransport only, no network, no real token."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

import telegram_bot as tb

TOKEN = "11:fake_token_for_tests"
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


def cb(data, chat=CHAT, user=CHAT, mid=7):
    return {"update_id": 2, "callback_query": {
        "id": "q1", "from": {"id": user}, "data": data,
        "message": {"message_id": mid, "chat": {"id": chat}}}}


def form(req):
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def last_json(rig, method):
    return json.loads(rig.named(method)[-1].content)


def press(rig, *datas, wait=False):
    """Callbacks in one loop; background tasks are awaited (wait) or cancelled."""
    async def go():
        for d in datas:
            await rig.bot.handle_update(cb(d))
        while wait and rig.bot._tasks:
            await asyncio.gather(*list(rig.bot._tasks))
        for t in list(rig.bot._tasks):
            t.cancel()
    run(go())


def test_url_message_opens_panel_and_start_queues_job(rig):
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": "abcdef12-3456", "status": "queued"})
    run(rig.bot.handle_update(msg("watch this https://youtu.be/abc, please")))
    assert rig.named("POST /api/process") == []  # nothing queued before Start
    panel = last_json(rig, "sendMessage")
    assert "https://youtu.be/abc" in panel["text"] and "Captions: default" in panel["text"]
    press(rig, "go")
    body = form(rig.named("POST /api/process")[0])
    assert body == {"url": "https://youtu.be/abc", "acknowledged": "true", "output_format": "vertical",
                    "layouts": "auto", "clip_min_seconds": "30", "clip_max_seconds": "60",
                    "captions": "default", "auto_hook": "true", "auto_hook_style": "pill"}
    assert "Queued job abcdef12" in rig.texts()[-1]
    assert rig.bot.jobs[str(CHAT)] == ["abcdef12-3456"]
    assert rig.bot.data["job_settings"]["abcdef12-3456"]["priv"] == "private"
    assert rig.bot.data["panels"][str(CHAT)] == {}


def test_start_sends_exactly_the_chosen_fields(rig):
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": "j1"})
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:cap:off", "s:lay:split", "s:fmt:square", "s:hook:off", "s:n:5",
          "s:len:60-90", "s:priv:public", "go")
    assert form(rig.named("POST /api/process")[0]) == {
        "url": "https://youtu.be/abc", "acknowledged": "true", "output_format": "square",
        "layouts": "split", "clip_min_seconds": "60", "clip_max_seconds": "90",
        "captions": "false", "auto_hook": "false", "target_clips": "5"}
    assert rig.bot.data["job_settings"]["j1"]["priv"] == "public"


def test_button_toggle_edits_panel_in_place(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "m:cap")
    sub = last_json(rig, "editMessageReplyMarkup")
    assert sub["message_id"] == 7
    labels = [b["text"] for row in sub["reply_markup"]["inline_keyboard"] for b in row]
    assert "✓ default" in labels and "hormozi" in labels and "off" in labels
    press(rig, "s:cap:hormozi")
    ed = last_json(rig, "editMessageText")
    assert ed["message_id"] == 7 and "Captions: hormozi" in ed["text"]
    assert any(b["text"] == "Captions: hormozi" for row in ed["reply_markup"]["inline_keyboard"] for b in row)
    press(rig, "s:cap:bogus")  # not an offered value: ignored
    assert rig.bot.data["panels"][str(CHAT)]["7"]["s"]["cap"] == "hormozi"
    assert len(rig.named("answerCallbackQuery")) == 3
    press(rig, "x")
    assert last_json(rig, "editMessageText")["text"] == "Cancelled."
    assert rig.bot.data["panels"][str(CHAT)] == {}


def test_callbacks_from_unlisted_chat_or_user_are_silent(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    rig.calls.clear()
    run(rig.bot.handle_update(cb("s:cap:off", chat=999, user=CHAT)))
    run(rig.bot.handle_update(cb("s:cap:off", chat=CHAT, user=999)))
    run(rig.bot.handle_update(cb("go", chat=CHAT, user=999)))
    assert rig.calls == []
    assert rig.bot.data["panels"][str(CHAT)]["7"]["s"]["cap"] == "default"


def _every_button(bot, job_id):
    s = dict(tb.DEFAULTS)
    boards = [bot.panel_keyboard(s), bot.presets_keyboard(CHAT),
              bot.clip_keyboard(job_id, 99, {"state": ""}),
              [[tb._btn("y", "ow:y"), tb._btn("n", "ow:n")]]]
    boards += [bot.option_keyboard(s, k) for k in tb.OPTIONS]
    return [b for board in boards for row in board for b in row]


def test_callback_data_fits_64_bytes(tmp_path):
    r = Rig(tmp_path, n8n_url="http://n8n.local/hook", n8n_secret="S")
    r.bot._presets_of(CHAT)[tb.preset_name("é" * 40)] = dict(tb.DEFAULTS)
    r.bot._presets_of(CHAT)[tb.preset_name("x" * 80)] = dict(tb.DEFAULTS)
    buttons = _every_button(r.bot, "0123456789abcdef0123456789abcdef0123")
    assert len(buttons) > 40
    for b in buttons:
        assert len(b["callback_data"].encode()) <= 64, b


def test_api_refusal_is_reported(rig):
    rig.routes["POST /api/process"] = httpx.Response(400, json={"detail": "URL not allowed"})
    run(rig.bot.handle_update(msg("https://10.0.0.1/x")))
    press(rig, "go")
    assert "URL not allowed" in rig.texts()[-1]


def test_expired_panel_says_so(rig):
    press(rig, "go")
    assert "expired" in last_json(rig, "answerCallbackQuery")["text"]
    assert rig.named("POST /api/process") == []


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
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 100, "file_name": "a.mp4"})))
    assert rig.named("getFile") == [] and "a.mp4" in rig.texts()[0]  # panel first
    press(rig, "s:fmt:horizontal", "go")
    req = rig.named("POST /api/process")[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in req.content and b"acknowledged" in req.content
    assert b'name="output_format"\r\n\r\nhorizontal' in req.content
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
    assert TOKEN not in text and "fake_token_for_tests" not in text


def test_run_from_env_silences_httpx_request_logs(caplog):
    """httpx logs request URLs at INFO; Bot API URLs contain the token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    caplog.set_level(logging.INFO)
    asyncio.run(tb.run_from_env(env={"TELEGRAM_INTAKE_TOKEN": TOKEN,
                                     "TELEGRAM_ALLOWED_CHAT_IDS": ""}))
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"ok": True}))) as c:
            await c.get(f"https://api.telegram.org/bot{TOKEN}/getMe")
    asyncio.run(call())
    assert TOKEN not in caplog.text


# --- presets ---------------------------------------------------------------
def _say_name(rig, name):
    run(rig.bot.handle_update(msg(name)))


def test_preset_save_load_overwrite_delete_default(tmp_path):
    rig = Rig(tmp_path)
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:hook:dark", "sv")
    assert "name" in rig.texts()[-1]
    _say_name(rig, "  My/Talk!!  preset ")
    assert rig.texts()[-1] == "Saved preset MyTalk preset."
    assert rig.bot.data["presets"][str(CHAT)]["MyTalk preset"]["hook"] == "dark"

    # Overwrite asks first; "No" keeps the old settings.
    press(rig, "s:hook:red", "sv")
    _say_name(rig, "MyTalk preset")
    ask = last_json(rig, "sendMessage")
    assert "Overwrite" in ask["text"] and ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ow:y"
    press(rig, "ow:n")
    assert rig.bot.data["presets"][str(CHAT)]["MyTalk preset"]["hook"] == "dark"
    press(rig, "sv")
    _say_name(rig, "MyTalk preset")
    press(rig, "ow:y")
    assert rig.bot.data["presets"][str(CHAT)]["MyTalk preset"]["hook"] == "red"
    assert "Saved preset" in last_json(rig, "editMessageText")["text"]

    # Load: list as buttons, then apply.
    press(rig, "s:hook:off", "ld")
    names = [b["callback_data"] for row in last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]
             for b in row]
    assert "l:MyTalk preset" in names
    press(rig, "l:MyTalk preset")
    assert "Hook: red" in last_json(rig, "editMessageText")["text"]

    # Default: new panels start from it; persisted across a restart.
    run(rig.bot.handle_update(msg("/preset_default MyTalk preset")))
    run(rig.bot.handle_update(msg("/presets")))
    assert "MyTalk preset (default)" in rig.texts()[-1]
    rig2 = Rig(tmp_path)
    run(rig2.bot.handle_update(msg("https://youtu.be/xyz")))
    assert "Hook: red" in rig2.texts()[-1]

    run(rig2.bot.handle_update(msg("/preset_delete MyTalk preset")))
    assert rig2.bot.data["presets"][str(CHAT)] == {} and str(CHAT) not in rig2.bot.data["default_preset"]
    run(rig2.bot.handle_update(msg("https://youtu.be/xyz")))
    assert "Hook: pill" in rig2.texts()[-1]
    run(rig2.bot.handle_update(msg("/preset_default nope")))
    assert "No preset" in rig2.texts()[-1]


def test_preset_name_is_sanitised_and_bounded():
    assert tb.preset_name("a\nb\x00c<script>") == "abcscript"
    assert len(tb.preset_name("é" * 100).encode()) <= tb.NAME_BYTES
    assert tb.preset_name("!!!") == ""


def test_command_or_link_does_not_become_a_preset_name(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "sv")
    run(rig.bot.handle_update(msg("https://youtu.be/other")))
    assert rig.bot.data["presets"].get(str(CHAT), {}) == {}
    assert "youtu.be/other" in rig.texts()[-1]  # a new panel instead


def test_load_with_no_presets_says_so(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "ld")
    assert "No presets" in last_json(rig, "answerCallbackQuery")["text"]


def test_stale_preset_values_fall_back_to_defaults():
    assert tb.clean_settings({"hook": "gone_style", "n": "5"}) == {**tb.DEFAULTS, "n": "5"}


# --- approval and n8n upload --------------------------------------------------
JOB = "jobid-1234"
N8N = "http://n8n.local/webhook/abc-secret-path"
SECRET = "s3cr3t-value"
CLIP = {"video_url": "/videos/jobid-1234/a.mp4", "video_title_for_youtube_short": "Big title",
        "video_description_for_instagram": "Watch this #ai #n8n #ai"}


def up_rig(tmp_path, **kw):
    kw.setdefault("n8n_url", N8N)
    kw.setdefault("n8n_secret", SECRET)
    r = Rig(tmp_path, send_files=True, **kw)
    r.routes["GET /videos/jobid-1234/a.mp4"] = httpx.Response(200, content=b"MP4DATA")
    r.routes[f"GET /api/status/{JOB}"] = status("completed", [CLIP])
    r.bot.data["job_settings"][JOB] = {**tb.DEFAULTS, "priv": "unlisted"}
    return r


def deliver_job(r):
    async def go():
        async with r.client() as c:
            await r.bot._deliver(c, CHAT, 0, CLIP, JOB)
    run(go())


def multipart_fields(req):
    ctype = req.headers["content-type"]
    boundary = ctype.split("boundary=")[1].encode()
    out = {}
    for part in req.content.split(b"--" + boundary)[1:-1]:
        head, _, val = part.strip(b"\r\n").partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        out[name] = val
    return out


def test_clip_has_approval_buttons_and_upload_posts_exact_contract(tmp_path):
    r = up_rig(tmp_path)
    r.routes["POST /webhook/abc-secret-path"] = httpx.Response(200, json={
        "ok": True, "drive_link": "https://drive.example/f", "youtube_url": "https://youtu.be/NEW"})
    deliver_job(r)
    sent = r.named("sendVideo")[0]
    kb = json.loads(multipart_fields(sent)["reply_markup"])["inline_keyboard"]
    datas = [b["callback_data"] for row in kb for b in row]
    assert datas == [f"u:{JOB}:0", f"k:{JOB}:0", f"t:{JOB}:0"]
    press(r, f"u:{JOB}:0", wait=True)
    post = r.named("POST /webhook/abc-secret-path")[0]
    assert post.headers["X-OpenShorts-Secret"] == SECRET
    assert post.headers["content-type"].startswith("multipart/form-data")
    f = multipart_fields(post)
    assert set(f) == {"video", "title", "description", "tags", "privacy", "job_id", "clip_index"}
    assert f["video"] == b"MP4DATA" and f["title"] == b"Big title"
    assert f["description"] == b"Watch this #ai #n8n #ai" and f["tags"] == b"ai,n8n"
    assert f["privacy"] == b"unlisted" and f["job_id"] == JOB.encode() and f["clip_index"] == b"0"
    caps = [json.loads(c.content)["caption"] for c in r.named("editMessageCaption")]
    assert "⏳ Uploading…" in caps[0]
    assert "✅ On YouTube: https://youtu.be/NEW (Drive: https://drive.example/f)" in caps[-1]
    assert last_json(r, "editMessageCaption")["reply_markup"]["inline_keyboard"] == []


def test_no_double_upload(tmp_path):
    r = up_rig(tmp_path)
    r.routes["POST /webhook/abc-secret-path"] = httpx.Response(200, json={
        "ok": True, "drive_link": "d", "youtube_url": "https://youtu.be/NEW"})
    deliver_job(r)
    press(r, f"u:{JOB}:0")
    press(r, f"u:{JOB}:0")  # still uploading (task cancelled by press())
    assert "Already uploading" in last_json(r, "answerCallbackQuery")["text"]
    r.bot.data["clips"][f"{JOB}:0"]["state"] = ""
    press(r, f"u:{JOB}:0", wait=True)
    press(r, f"u:{JOB}:0")
    assert "Already uploaded: https://youtu.be/NEW" == last_json(r, "answerCallbackQuery")["text"]
    assert len(r.named("POST /webhook/abc-secret-path")) == 1


def test_upload_hidden_when_webhook_unset(tmp_path):
    r = up_rig(tmp_path, n8n_url="")
    deliver_job(r)
    assert "reply_markup" not in multipart_fields(r.named("sendVideo")[0])
    run(r.bot.handle_update(msg("/help")))
    assert "N8N_UPLOAD_WEBHOOK_URL is not set" in r.texts()[-1]
    press(r, f"u:{JOB}:0")
    assert r.named("POST /webhook/abc-secret-path") == []


def test_edit_title_then_upload_uses_it(tmp_path):
    r = up_rig(tmp_path)
    r.routes["POST /webhook/abc-secret-path"] = httpx.Response(200, json={"ok": True, "youtube_url": "y"})
    deliver_job(r)
    press(r, f"t:{JOB}:0")
    assert "new title" in r.texts()[-1]
    run(r.bot.handle_update(msg("  A   better\ntitle " + "x" * 200)))
    rec = r.bot.data["clips"][f"{JOB}:0"]
    assert rec["title"].startswith("A better title x") and len(rec["title"]) == 100
    assert last_json(r, "editMessageCaption")["caption"].startswith("1. A better title")
    press(r, f"u:{JOB}:0", wait=True)
    assert multipart_fields(r.named("POST /webhook/abc-secret-path")[0])["title"] == rec["title"].encode()


def test_skip_removes_buttons(tmp_path):
    r = up_rig(tmp_path)
    deliver_job(r)
    press(r, f"k:{JOB}:0")
    ed = last_json(r, "editMessageCaption")
    assert "⏭ Skipped" in ed["caption"] and ed["reply_markup"]["inline_keyboard"] == []


def test_n8n_error_surfaces_without_secrets(tmp_path, caplog):
    r = up_rig(tmp_path)
    r.routes["POST /webhook/abc-secret-path"] = httpx.Response(200, json={
        "ok": False, "error": f"quota exceeded calling {N8N} with {SECRET}"})
    deliver_job(r)
    press(r, f"u:{JOB}:0", wait=True)
    cap = last_json(r, "editMessageCaption")["caption"]
    assert "Upload failed: quota exceeded" in cap and SECRET not in cap and N8N not in cap
    # Network failure: the exception text (with the URL) never reaches the chat or logs.
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} {SECRET}")
    r.routes["POST /webhook/abc-secret-path"] = boom
    with caplog.at_level(logging.DEBUG):
        press(r, f"u:{JOB}:0", wait=True)
    cap = last_json(r, "editMessageCaption")["caption"]
    assert "did not answer" in cap
    for text in (cap, caplog.text):
        assert SECRET not in text and "abc-secret-path" not in text and TOKEN not in text
    # A failed upload offers the buttons again.
    assert last_json(r, "editMessageCaption")["reply_markup"]["inline_keyboard"]


def test_clip_callbacks_from_unlisted_user_are_silent(tmp_path):
    r = up_rig(tmp_path)
    deliver_job(r)
    r.calls.clear()
    run(r.bot.handle_update(cb(f"u:{JOB}:0", user=999)))
    assert r.calls == []
