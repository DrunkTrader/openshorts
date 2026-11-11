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
Messages from any other chat get no reply at all.

This is separate from the admin-alert bot (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) used by the Cloud edition: use a different bot and token.

## Use

- Send a link: it replies "Queued job <id>", then edits that one message as the
  job moves from queued to processing to done, then sends each clip.
- Send a video file: up to 20 MB (the Bot API limit for downloads). For bigger
  videos send a link.
- `/schedule <job> <clip#> <platforms> <YYYY-MM-DD HH:MM>`: schedule a clip
  through Upload-Post. Needs `UPLOAD_POST_API_KEY` and
  `TELEGRAM_UPLOAD_POST_PROFILE`. `<job>` is the short id from "Queued".
  Platforms: `tiktok,instagram,youtube`. Times use `TELEGRAM_TIMEZONE`
  (default UTC; on Windows install `tzdata`, which requirements.txt does).
- `/scheduled` lists pending posts, `/cancel <id>` cancels one.
- `/help` shows the commands.

## Clip delivery

By default (`TELEGRAM_SEND_FILES=1`) the bot downloads each clip from the local
API and sends it as a video (or a document), up to 50 MB each (Bot API limit).
That works with no public address. Links are sent only when `PUBLIC_API_URL` is
set and a clip could not be sent as a file (or `TELEGRAM_SEND_FILES=0`): the
signed `download_url` as the API returns it, or `PUBLIC_API_URL` plus the clip
path when there is no media secret.

## Limits

- Self-host only: the bot is not started when `BILLING_ENABLED` is on.
- There is no "process this later" command: jobs start when queued. Schedule the
  posting of a finished clip instead.
- `TELEGRAM_API_BASE` overrides the API address (default
  `http://127.0.0.1:$PORT`).
