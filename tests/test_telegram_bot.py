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


def msg(text=None, chat=CHAT, user=None, **extra):
    m = {"chat": {"id": chat}, "from": {"id": chat if user is None else user}}
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
    boards += [bot.option_keyboard(s, k) for k in {**tb.OPTIONS, **tb.SPLIT_OPTIONS}]
    long_items = [{"id": c * 32, "name": "é" * 80} for c in "0123456789abcdef"]
    s_on = {**s, "spl": "on", "src": {"id": "f" * 32, "name": "é" * 64}}
    boards += [bot.split_keyboard(s_on), bot.split_keyboard(s), bot.source_keyboard(),
               bot.pick_keyboard(long_items), bot.library_keyboard(long_items), bot.panel_keyboard(s_on)]
    boards += [bot.cancel_keyboard(job_id, w) for w in "sl"] + [bot.confirm_keyboard(job_id, w) for w in "sl"]
    return [b for board in boards for row in board for b in row]


def test_callback_data_fits_64_bytes(tmp_path):
    r = Rig(tmp_path, n8n_url="http://n8n.local/hook", n8n_secret="S")
    r.bot._presets_of(CHAT)[tb.preset_name("é" * 40)] = dict(tb.DEFAULTS)
    r.bot._presets_of(CHAT)[tb.preset_name("x" * 80)] = dict(tb.DEFAULTS)
    buttons = _every_button(r.bot, "0123456789abcdef0123456789abcdef0123")
    assert len(buttons) > 80
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


def test_upload_disabled_when_webhook_secret_missing(monkeypatch, tmp_path):
    """A webhook URL without its secret must not be used (fail closed)."""
    seen = {}

    async def fake_run(self, stop, is_active):
        seen["url"] = self.n8n_url

    monkeypatch.setattr(tb.IntakeBot, "run", fake_run)
    base = {"TELEGRAM_INTAKE_TOKEN": TOKEN, "TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT),
            "TELEGRAM_STATE_PATH": str(tmp_path / "s.json"),
            "N8N_UPLOAD_WEBHOOK_URL": "http://127.0.0.1:9/hook"}
    asyncio.run(tb.run_from_env(env=dict(base)))
    assert seen["url"] == ""
    asyncio.run(tb.run_from_env(env={**base, "N8N_WEBHOOK_SECRET": "s3cret"}))
    assert seen["url"] == "http://127.0.0.1:9/hook"


# --- split screen -------------------------------------------------------------
LIB_ID, LIB_ID2 = "a" * 32, "b" * 32
LIB = {"items": [{"id": LIB_ID, "name": "Gameplay", "duration": 30.0, "has_audio": True},
                 {"id": LIB_ID2, "name": "Rain", "duration": 12.0, "has_audio": False}]}


def send(rig, update, wait=True):
    """One update; background tasks awaited (wait) or cancelled."""
    async def go():
        await rig.bot.handle_update(update)
        while wait and rig.bot._tasks:
            await asyncio.gather(*list(rig.bot._tasks))
        for t in list(rig.bot._tasks):
            t.cancel()
    run(go())


def panel_s(rig, mid="7"):
    return rig.bot.data["panels"][str(CHAT)][mid]["s"]


def kb_datas(markup):
    return [b["callback_data"] for row in markup for b in row]


def test_split_submenu_forces_vertical_and_needs_a_source(rig):
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json=LIB)
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    assert "Split screen: off" in rig.texts()[-1]
    press(rig, "s:fmt:square", "m:spl")
    datas = kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"])
    assert {"s:spl:off", "s:spl:on", "m:src", "m:pos", "m:rat", "m:aud", "m:off", "m:"} <= set(datas)
    press(rig, "s:spl:on")
    ed = last_json(rig, "editMessageText")
    assert "Format: vertical (split screen needs it)" in ed["text"]
    assert "Split screen: on, extra video not picked yet at the bottom" in ed["text"]
    assert panel_s(rig)["fmt"] == "vertical"
    press(rig, "s:fmt:horizontal")
    assert "needs vertical" in last_json(rig, "answerCallbackQuery")["text"]
    assert panel_s(rig)["fmt"] == "vertical"
    press(rig, "go")
    assert "Pick the extra video" in last_json(rig, "answerCallbackQuery")["text"]
    assert rig.named("POST /api/process") == []
    # Position / ratio / audio / offset menus, and their Back goes to the split menu.
    press(rig, "m:off")
    datas = kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"])
    assert datas == ["s:off:0", "s:off:10", "s:off:30", "s:off:60", "sw:o", "m:spl"]
    press(rig, "s:pos:top", "s:rat:60/40", "s:aud:mix", "s:off:30", "s:rat:70/30")
    assert {k: panel_s(rig)[k] for k in ("pos", "rat", "aud", "off")} == {
        "pos": "top", "rat": "60/40", "aud": "mix", "off": "30"}


def test_typed_offset_is_validated(rig):
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "sw:o")
    run(rig.bot.handle_update(msg("-3")))
    assert "not a number" in rig.texts()[-1] and panel_s(rig)["off"] == "0"
    press(rig, "sw:o")
    run(rig.bot.handle_update(msg("12.50")))
    assert panel_s(rig)["off"] == "12.5" and "12.5 s" in rig.texts()[-1]
    assert tb.parse_offset("nan") is None and tb.parse_offset("1e9") is None


def test_library_source_preset_and_deleted_item(tmp_path):
    rig = Rig(tmp_path)
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json=LIB)
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:spl:on", "m:src")
    assert kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]) == [
        "sl", "sw:v", "sw:u", "m:spl"]
    press(rig, "sl")
    assert kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]) == [
        f"sp:{LIB_ID}", f"sp:{LIB_ID2}", "m:src"]
    press(rig, f"sp:{LIB_ID}", "s:rat:60/40", "sv")
    run(rig.bot.handle_update(msg("gp")))
    saved = rig.bot.data["presets"][str(CHAT)]["gp"]
    assert saved["src"] == {"id": LIB_ID, "name": "Gameplay"} and saved["spl"] == "on"
    assert set(saved) == set(tb.DEFAULTS)  # an id and a name, never file bytes
    run(rig.bot.handle_update(msg("/preset_default gp")))
    # Still in the library: split stays on.
    run(rig.bot.handle_update(msg("https://youtu.be/two")))
    assert "Split screen: on, extra video Gameplay at the bottom, 60/40" in rig.texts()[-1]
    # Deleted since: the panel says so and split is off for this run only.
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json={"items": []})
    run(rig.bot.handle_update(msg("https://youtu.be/three")))
    text = rig.texts()[-1]
    assert "no longer in the library" in text and "Split screen: off" in text
    assert rig.bot.data["presets"][str(CHAT)]["gp"]["spl"] == "on"
    # Loading the preset on a panel checks again.
    press(rig, "l:gp")
    assert "no longer in the library" in last_json(rig, "editMessageText")["text"]
    # A picked item gone since the list was shown.
    press(rig, f"sp:{LIB_ID}")
    assert "gone" in last_json(rig, "answerCallbackQuery")["text"]


def _tg_file(rig, size=100):
    rig.routes["getFile"] = ok({"file_path": "videos/f.mp4"})
    rig.routes["f.mp4"] = httpx.Response(200, content=b"x" * size)  # Telegram routes key on the last segment


def test_sent_video_becomes_the_extra_source(rig):
    _tg_file(rig)
    rig.routes["POST /api/slot-uploads"] = httpx.Response(200, json={"id": LIB_ID2, "name": "loop"})
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:spl:on", "sw:v")
    assert "20 MB" in rig.texts()[-1]
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 21 * 1024 * 1024})))
    assert "over 20 MB" in rig.texts()[-1] and rig.named("getFile") == []
    press(rig, "sw:v")
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 100, "file_name": "loop.mp4"})))
    up = rig.named("POST /api/slot-uploads")[0]
    f = multipart_fields(up)
    assert f["name"] == b"loop" and f["file"] == b"x" * 100
    assert b'filename="telegram.mp4"' in up.content
    assert panel_s(rig)["src"] == {"id": LIB_ID2, "name": "loop"}
    assert len(rig.bot.data["panels"][str(CHAT)]) == 1  # no second panel for the video
    assert rig.named("POST /api/process") == []


def test_link_goes_to_the_api_and_the_bot_never_fetches_it(rig):
    rig.routes["POST /api/slot-uploads"] = httpx.Response(200, json={"id": LIB_ID, "name": "Some title"})
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:spl:on", "sw:u")
    rig.calls.clear()
    send(rig, msg("here https://www.youtube.com/watch?v=extra1"))
    hosts = {c.url.host for c in rig.calls}
    assert hosts <= {"api.telegram.org", "127.0.0.1"}
    (up,) = rig.named("POST /api/slot-uploads")
    assert form(up) == {"url": "https://www.youtube.com/watch?v=extra1"}
    assert panel_s(rig)["src"] == {"id": LIB_ID, "name": "Some title"}
    assert len(rig.bot.data["panels"][str(CHAT)]) == 1  # the link did not open a new panel
    assert "Extra video ready: Some title" in rig.texts()[-1]
    # A refusal is reported.
    rig.routes["POST /api/slot-uploads"] = httpx.Response(400, json={"detail": "This link is a playlist."})
    press(rig, "sw:u")
    send(rig, msg("https://www.youtube.com/playlist?list=x"))
    assert "Could not use that link: This link is a playlist." in rig.texts()[-1]


def test_library_lists_imports_and_deletes(rig):
    rig.routes["POST /api/slot-uploads/import"] = httpx.Response(
        200, json={"imported": ["new.mp4"], "skipped": ["bad.mp4"]})
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json=LIB)
    run(rig.bot.handle_update(msg("/library")))
    assert [c.url.path for c in rig.calls if "127.0.0.1" in c.url.host] == [
        "/api/slot-uploads/import", "/api/slot-uploads"]
    out = last_json(rig, "sendMessage")
    assert "Imported from the folder: new.mp4" in out["text"] and "bad.mp4" in out["text"]
    assert "1. Gameplay (30 s)" in out["text"] and "2. Rain (12 s)" in out["text"]
    assert kb_datas(out["reply_markup"]["inline_keyboard"]) == [f"lx:{LIB_ID}", f"lx:{LIB_ID2}"]
    rig.routes[f"DELETE /api/slot-uploads/{LIB_ID}"] = httpx.Response(200, json={"success": True})
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json={"items": LIB["items"][1:]})
    press(rig, f"lx:{LIB_ID}")
    assert len(rig.named(f"DELETE /api/slot-uploads/{LIB_ID}")) == 1
    ed = last_json(rig, "editMessageText")
    assert "Gameplay" not in ed["text"] and "Rain" in ed["text"]
    assert last_json(rig, "answerCallbackQuery")["text"] == "Deleted."
    press(rig, "lx:../../etc")
    assert not [c for c in rig.calls if c.method == "DELETE" and "etc" in str(c.url)]


def test_save_caption_adds_to_library(rig):
    _tg_file(rig)
    rig.routes["POST /api/slot-uploads"] = httpx.Response(200, json={"id": LIB_ID, "name": "My Loop"})
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 100}, caption="/save  My/Loop!")))
    assert multipart_fields(rig.named("POST /api/slot-uploads")[0])["name"] == b"MyLoop"
    assert rig.texts()[-1] == "Saved My Loop in the library."
    assert rig.bot.data["panels"].get(str(CHAT), {}) == {}
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 100}, caption="/save !!")))
    assert "Add a name" in rig.texts()[-1]


def test_new_messages_and_callbacks_from_unlisted_sender_are_silent(rig):
    send(rig, msg("/library", user=999))
    send(rig, msg("https://youtu.be/abc", user=999))
    run(rig.bot.handle_update(msg(video={"file_id": "f", "file_size": 1}, caption="/save x", user=999)))
    for data in ("sl", f"sp:{LIB_ID}", "sw:u", f"lx:{LIB_ID}", "m:spl", "s:spl:on"):
        run(rig.bot.handle_update(cb(data, user=999)))
        run(rig.bot.handle_update(cb(data, chat=999)))
    assert rig.calls == []


SPLIT_JOB = "splitjob-1"
PLAIN = {"video_url": f"/videos/{SPLIT_JOB}/a.mp4", "video_title_for_youtube_short": "T"}
SPLIT = {"video_url": f"/videos/{SPLIT_JOB}/recut_1_x_a.mp4", "video_title_for_youtube_short": "T"}


def split_rig(tmp_path, split_response=None, **settings):
    r = Rig(tmp_path, send_files=True, n8n_url=N8N, n8n_secret=SECRET)
    done = {"split": False}

    def do_split(req):
        if split_response is not None:
            return split_response
        done["split"] = True
        return httpx.Response(200, json={"success": True, "new_video_url": SPLIT["video_url"]})
    r.routes["POST /api/clip/split"] = do_split
    r.routes[f"GET /api/status/{SPLIT_JOB}"] = lambda req: status(
        "completed", [SPLIT if done["split"] else PLAIN])
    r.routes[f"GET {PLAIN['video_url']}"] = httpx.Response(200, content=b"PLAINDATA")
    r.routes[f"GET {SPLIT['video_url']}"] = httpx.Response(200, content=b"SPLITDATA")
    r.bot.data["job_settings"][SPLIT_JOB] = tb.clean_settings({
        **tb.DEFAULTS, "spl": "on", "src": {"id": LIB_ID, "name": "Gameplay"}, "off": "10", **settings})
    return r


@pytest.mark.parametrize("pos,rat,top,bottom,ratio", [
    ("bottom", "50/50", "source", "upload", "50/50"),
    ("bottom", "60/40", "source", "upload", "60/40"),
    ("top", "50/50", "upload", "source", "50/50"),
    ("top", "60/40", "upload", "source", "40/60"),  # 60% stays on the main clip
])
def test_split_applied_after_completion_with_exact_body(tmp_path, pos, rat, top, bottom, ratio):
    r = split_rig(tmp_path, pos=pos, rat=rat, aud="extra")
    run(r.bot.track(CHAT, SPLIT_JOB, 7))
    body = json.loads(r.named("POST /api/clip/split")[0].content)
    extra = {"kind": "upload", "upload_id": LIB_ID, "offset_s": 10.0}
    assert body == {"job_id": SPLIT_JOB, "clip_index": 0, "slots": {
        "top": extra if top == "upload" else {"kind": "source"},
        "bottom": extra if bottom == "upload" else {"kind": "source"},
        "ratio": ratio, "audio": "upload", "loop": True}}
    assert multipart_fields(r.named("sendVideo")[0])["video"] == b"SPLITDATA"
    # Approval and the n8n upload use the split file.
    r.routes["POST /webhook/abc-secret-path"] = httpx.Response(200, json={"ok": True, "youtube_url": "y"})
    press(r, f"u:{SPLIT_JOB}:0", wait=True)
    assert multipart_fields(r.named("POST /webhook/abc-secret-path")[0])["video"] == b"SPLITDATA"


def test_split_failure_sends_plain_clip_with_short_note(tmp_path):
    r = split_rig(tmp_path, split_response=httpx.Response(
        500, json={"detail": "ffmpeg failed: /srv/app/output/x " + TOKEN}))
    run(r.bot.track(CHAT, SPLIT_JOB, 7))
    sent = multipart_fields(r.named("sendVideo")[0])
    assert sent["video"] == b"PLAINDATA"
    cap = sent["caption"].decode()
    assert "Split screen failed (the render failed), so this is the plain clip." in cap
    assert "/srv" not in cap and TOKEN not in cap
    # A 4xx detail is safe to show.
    r2 = split_rig(tmp_path / "b", split_response=httpx.Response(
        404, json={"detail": "Upload not found in your library"}))
    run(r2.bot.track(CHAT, SPLIT_JOB, 7))
    assert "Upload not found in your library" in multipart_fields(r2.named("sendVideo")[0])["caption"].decode()


def test_split_off_job_never_calls_split(tmp_path):
    r = split_rig(tmp_path, spl="off")
    run(r.bot.track(CHAT, SPLIT_JOB, 7))
    assert r.named("POST /api/clip/split") == []
    assert multipart_fields(r.named("sendVideo")[0])["video"] == b"PLAINDATA"


def test_start_with_split_remembers_it_per_job(rig):
    rig.routes["GET /api/slot-uploads"] = httpx.Response(200, json=LIB)
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": "j-split"})
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "s:fmt:square", "s:spl:on", f"sp:{LIB_ID}", "s:pos:top", "go")
    assert form(rig.named("POST /api/process")[0])["output_format"] == "vertical"
    js = rig.bot.data["job_settings"]["j-split"]
    assert js["spl"] == "on" and js["src"]["id"] == LIB_ID and js["pos"] == "top"


# --- cancelling jobs -------------------------------------------------------------
CJ = "0123abcd-1111-2222-3333-444455556666"


def test_queued_message_has_cancel_and_confirmation_flow(rig):
    rig.routes["POST /api/process"] = httpx.Response(200, json={"job_id": CJ})
    run(rig.bot.handle_update(msg("https://youtu.be/abc")))
    press(rig, "go")
    queued = last_json(rig, "sendMessage")
    assert queued["text"] == "Queued job 0123abcd"
    assert kb_datas(queued["reply_markup"]["inline_keyboard"]) == [f"cj:s:{CJ}"]
    press(rig, f"cj:s:{CJ}")
    assert kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]) == [
        f"cy:s:{CJ}", f"cn:s:{CJ}"]
    press(rig, f"cn:s:{CJ}")
    assert kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]) == [
        f"cj:s:{CJ}"]
    assert rig.named(f"POST /api/jobs/{CJ}/cancel") == []
    rig.routes[f"POST /api/jobs/{CJ}/cancel"] = httpx.Response(200, json={"job_id": CJ, "status": "cancelled"})
    press(rig, f"cy:s:{CJ}")
    assert len(rig.named(f"POST /api/jobs/{CJ}/cancel")) == 1
    ed = last_json(rig, "editMessageText")
    assert ed["text"] == "Job 0123abcd: cancelled" and ed["reply_markup"]["inline_keyboard"] == []
    assert last_json(rig, "answerCallbackQuery")["text"] == "Cancelled."


def test_cancel_refused_says_why(rig):
    rig.routes[f"POST /api/jobs/{CJ}/cancel"] = httpx.Response(
        409, json={"detail": "This job is already completed: nothing to cancel."})
    press(rig, f"cy:s:{CJ}")
    assert "already completed" in last_json(rig, "answerCallbackQuery")["text"]
    press(rig, "cy:s:../../api/x", "cy:q:" + CJ)
    assert len([c for c in rig.calls if c.url.path.endswith("/cancel")]) == 1


def test_tracking_keeps_cancel_button_then_stops_on_cancel_without_clips(rig):
    seq = iter([status("queued"), status("processing"), status("cancelled"),
                status("completed", [{"video_url": "/videos/x/a.mp4"}])])
    rig.routes[f"GET /api/status/{CJ}"] = lambda r: next(seq)
    run(rig.bot.track(CHAT, CJ, 7))
    edits = [json.loads(c.content) for c in rig.named("editMessageText")]
    assert [e["text"] for e in edits] == ["Job 0123abcd: processing", "Job 0123abcd: cancelled"]
    assert kb_datas(edits[0]["reply_markup"]["inline_keyboard"]) == [f"cj:s:{CJ}"]
    assert edits[1]["reply_markup"]["inline_keyboard"] == []
    assert len(rig.named(f"GET /api/status/{CJ}")) == 3   # polling stopped
    assert rig.named("sendVideo") == [] and rig.texts() == []


def test_done_message_drops_the_cancel_button(rig):
    rig.routes[f"GET /api/status/{CJ}"] = status("completed", [])
    run(rig.bot.track(CHAT, CJ, 7))
    assert last_json(rig, "editMessageText")["reply_markup"]["inline_keyboard"] == []


JOBS = {"jobs": [
    {"job_id": CJ, "status": "processing", "stage": "Transcribing audio", "source": "https://youtu.be/a",
     "position": None, "started_at": 1.0},
    {"job_id": "9999ffff-aaaa", "status": "queued", "position": 2, "source": "talk.mp4", "stage": "x"},
    {"job_id": "8888eeee-bbbb", "status": "processing", "stage": "reading /srv/app/output/x.log"}]}


def test_jobs_lists_active_with_cancel_and_refreshes_after(rig):
    rig.routes["GET /api/jobs/active"] = httpx.Response(200, json=JOBS)
    run(rig.bot.handle_update(msg("/jobs")))
    out = last_json(rig, "sendMessage")
    assert out["text"].splitlines() == [
        "Active jobs:",
        "1. 0123abcd processing: Transcribing audio https://youtu.be/a",
        "2. 9999ffff queued, #2 in line talk.mp4",
        "3. 8888eeee processing"]   # a stage with a path is not shown
    assert kb_datas(out["reply_markup"]["inline_keyboard"]) == [
        f"cj:l:{CJ}", "cj:l:9999ffff-aaaa", "cj:l:8888eeee-bbbb"]
    press(rig, "cj:l:9999ffff-aaaa")
    assert kb_datas(last_json(rig, "editMessageReplyMarkup")["reply_markup"]["inline_keyboard"]) == [
        "cy:l:9999ffff-aaaa", "cn:l:9999ffff-aaaa"]
    rig.routes["POST /api/jobs/9999ffff-aaaa/cancel"] = httpx.Response(200, json={})
    rig.routes["GET /api/jobs/active"] = httpx.Response(200, json={"jobs": JOBS["jobs"][:1]})
    press(rig, "cy:l:9999ffff-aaaa")
    ed = last_json(rig, "editMessageText")
    assert "9999ffff" not in ed["text"] and "0123abcd" in ed["text"]
    assert kb_datas(ed["reply_markup"]["inline_keyboard"]) == [f"cj:l:{CJ}"]
    rig.routes["GET /api/jobs/active"] = httpx.Response(200, json={"jobs": []})
    run(rig.bot.handle_update(msg("/jobs")))
    assert rig.texts()[-1] == "No jobs queued or running."


def test_help_names_both_cancels(rig):
    run(rig.bot.handle_update(msg("/help")))
    assert "/jobs" in rig.texts()[-1] and "/cancel <id> - cancel a scheduled post" in rig.texts()[-1]


def test_cancel_callbacks_and_jobs_from_unlisted_sender_are_silent(rig):
    run(rig.bot.handle_update(msg("/jobs", user=999)))
    for data in (f"cj:s:{CJ}", f"cy:s:{CJ}", f"cn:l:{CJ}", f"cy:l:{CJ}"):
        run(rig.bot.handle_update(cb(data, user=999)))
        run(rig.bot.handle_update(cb(data, chat=999)))
    assert rig.calls == []
