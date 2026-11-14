# Telegram intake bot (self-host)

Send a video link (or a small video file) to your own Telegram bot and get the
finished clips back in the same chat. It is a long-polling client of the local
API: no inbound port, no public URL, no bot framework, and every API guard
(URL validation, SSRF checks, signed media links) still applies.

## Setup

1. Create a bot with BotFather and copy its token.
2. Find your chat id (message the bot, then ask the Bot API for updates, or use
   any "get my id" bot).
3. Put these in `.env`, then start the API (`uvicorn app:app`):

```
TELEGRAM_INTAKE_TOKEN=<token from BotFather>
TELEGRAM_ALLOWED_CHAT_IDS=<your chat id, comma-separated for several>
```

The bot starts with the API and stops with it (and while it drains for a
deploy). It is off unless the token is set AND the allowlist is not empty.
Messages and button presses get no reply at all unless both the chat id and the
sender's user id are in the allowlist (in a private chat they are the same id;
in a group, list the members' user ids too).

This is separate from the admin-alert bot (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) used by the Cloud edition: use a different bot and token.

## Use

- Send a link or a video file (up to 20 MB, the Bot API limit for downloads;
  for bigger videos send a link): the bot answers with a settings panel, one
  message it edits in place. Nothing is queued until you press Start.
- After Start it replies "Queued job <id>", then edits that one message as the
  job moves from queued to processing to done, then sends each clip.
- `/schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM>`: schedule a clip
  through Upload-Post. Needs `UPLOAD_POST_API_KEY` and
  `TELEGRAM_UPLOAD_POST_PROFILE`. `<job>` is the short id from "Queued".
  Platforms: `tiktok,instagram,youtube`. Times use `TELEGRAM_TIMEZONE`
  (default UTC; on Windows install `tzdata`, which requirements.txt does).
- `/scheduled` lists pending posts, `/cancel <id>` cancels one.
- `/library` lists the split-screen library with a 🗑 delete button per video
  (it imports the server's import folder first, see below). Send a video or
  a video file with the caption `/save <name>` to add it (name rules as for
  presets).
- `/help` shows the commands.

## Settings panel

Each line of the panel is a button that opens its choices (the current one
ticked); picking one edits the panel and goes back.

| Setting | Choices (default first) | Sent to `/api/process` as |
| --- | --- | --- |
| Captions | default, off, or any `subtitles.CAPTION_PRESETS` look | `captions` (`false` or the preset name) |
| Layout | auto, plain crop, split, screencast, speaker cut | `layouts` (`auto`, `none`, ...) |
| Format | vertical, square, horizontal | `output_format` |
| Hook | pill, off, or any `hooks.HOOK_STYLES` style | `auto_hook` + `auto_hook_style` |
| Clips | auto, 3, 5, 8, 10 | `target_clips` (left out for auto) |
| Length | 30-60, 15-30, 30-90, 60-90 s | `clip_min_seconds` / `clip_max_seconds` |
| YouTube privacy | private, unlisted, public | not sent; used by the upload below |
| Split screen | off, on | not sent; applied after the job (below) |

Actions: Start, Save as preset, Load preset, Cancel. Every button press is
answered; presses from a chat OR a user not in `TELEGRAM_ALLOWED_CHAT_IDS` get
no answer at all (in a group, list the members' user ids too).

## Split screen

Turning Split screen on opens a sub-menu and forces Format to vertical (the
split renders 9:16 only; the panel says so, and picking another format is
refused until split is off). Start is refused until an extra video is picked.

- Extra video: 📚 Library (the items of `GET /api/slot-uploads` as buttons),
  📤 Send a video (the next video or video file; 20 MB at most, the Bot API
  download limit), or 🔗 Send a link (the next message with a URL: the bot
  never downloads it, it posts the URL to `POST /api/slot-uploads`, and the
  API downloads the first `SLOT_UPLOAD_MAX_SECONDS` under the job download
  rules). A sent video or link is added to the library, so it shows in
  `/library` and counts towards `SLOT_LIBRARY_MAX_ITEMS`.
- Extra video at: bottom (default) or top.
- Ratio: 50/50 or 60/40. The 60% always goes to the clip: with the extra video
  on top the API gets `40/60`.
- Audio: source (default), extra (`upload`) or mix.
- Start offset in the extra video: 0, 10, 30, 60 s, or Type seconds (the next
  text message, a number from 0).

When the job completes, each clip goes through `POST /api/clip/split` (it is
synchronous; the bot waits up to 30 minutes per clip) with the clip in one slot,
the library item in the other, the ratio, audio, offset and `loop: true`. The
bot then reads the clip back from `/api/status` (its `video_url` now names the
split render) and sends that file; the approval buttons and the n8n upload use
it too. If the split fails the plain clip is sent with a short note (an API
4xx detail, otherwise just "the render failed").

The split settings are part of the panel settings, so presets keep them, with
the library item's id and name only. A preset naming an item deleted since
opens with split off for that run and a note in the panel; the preset itself
is unchanged.

## Presets

- Save as preset: the next text message is the name (letters, digits, spaces,
  `_` and `-`, up to 32 bytes). An existing name asks before overwriting.
- Load preset: lists the saved presets as buttons.
- `/presets` lists them, `/preset_delete <name>` deletes one,
  `/preset_default <name>` makes new panels start from it. Without a default,
  panels start from the defaults above.

Presets are per chat, in the bot's state file.

## Approval and upload to YouTube (n8n)

Set `N8N_UPLOAD_WEBHOOK_URL` (and `N8N_WEBHOOK_SECRET`) to get three buttons
under each clip: Upload to YouTube, Skip, Edit title. Without the URL the
clips come without buttons and `/help` says so.

- Edit title: the next text message becomes the clip's title (100 chars max).
- Upload: the clip's message turns to "⏳ Uploading…", then to
  "✅ On YouTube: <url> (Drive: <link>)", or a short error. A clip already
  uploaded (or uploading) is not sent again. A failed upload can be retried.

The webhook contract (fixed with the n8n workflow):

- `POST` multipart/form-data to `N8N_UPLOAD_WEBHOOK_URL`, header
  `X-OpenShorts-Secret: <N8N_WEBHOOK_SECRET>`, timeout 10 minutes.
- Fields: `video` (the mp4, fetched from the local API), `title` (100 chars
  max), `description` (the clip's description, else its title; 5000 chars
  max), `tags` (the description's hashtags, comma-separated, may be empty),
  `privacy` (`private|unlisted|public`, from the job's panel), `job_id`,
  `clip_index` (0-based, like `/schedule`'s API call).
- Answer JSON: `{"ok": true, "drive_link": "...", "youtube_url": "..."}` or
  `{"ok": false, "error": "..."}`. The error is shown in the chat with the
  secret and the webhook URL masked.

## State file

`TELEGRAM_STATE_PATH` (default `output/.telegram_state.json`), written
atomically:

```
{
  "offset": 0,                      # next getUpdates offset
  "jobs": {"<chat>": ["<job id>"]}, # last 20 per chat, for /schedule
  "panels": {"<chat>": {"<message id>": {"src": {"url": ...} | {"file": {...}}, "s": {settings}}}},
  "presets": {"<chat>": {"<name>": {settings}}},
  "default_preset": {"<chat>": "<name>"},
  "awaiting": {"<chat>": {"kind": "name" | "overwrite" | "title" | "split_video"
                          | "split_link" | "split_offset", ...}},
  "job_settings": {"<job id>": {settings}},          # last 200
  "clips": {"<job id>:<index>": {"chat", "msg", "kind", "i", "title", "link",
                                 "state", "youtube_url", "drive_link", "error"}}  # last 500
}
```

`settings` is `{"cap", "lay", "fmt", "hook", "n", "len", "priv", "spl", "pos",
"rat", "aud", "off", "src"}`; `src` is `{"id", "name"}` of a library item or
null.

## Clip delivery

By default (`TELEGRAM_SEND_FILES=1`) the bot downloads each clip from the local
API and sends it as a video (or a document), up to 50 MB each (Bot API limit).
That works with no public address. Links are sent only when `PUBLIC_API_URL` is
set and a clip could not be sent as a file (or `TELEGRAM_SEND_FILES=0`): the
signed `download_url` as the API returns it, or `PUBLIC_API_URL` plus the clip
path when there is no media secret.

## Library import folder

Video files (`.mp4 .mov .webm .mkv`) put directly in `SLOT_LIBRARY_IMPORT_DIR`
(default `split_library/` at the repo root, git-ignored) are imported into the
self-host library when the API starts and on `POST /api/slot-uploads/import`
(which `/library` calls). The library name is the file stem. Each file gets the
same checks as an upload; a valid one is moved out of the folder, an invalid
one stays there and is reported by name. Subdirectories and symlinks are never
read. Not available in the Cloud edition (404).

## Limits

- Self-host only: the bot is not started when `BILLING_ENABLED` is on.
- There is no "process this later" command: jobs start when queued. Schedule the
  posting of a finished clip instead.
- `TELEGRAM_API_BASE` overrides the API address (default
  `http://127.0.0.1:$PORT`).
