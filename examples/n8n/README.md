# OpenShorts + n8n

Importable workflows, from a one-shot clipper to a full channel autopilot.
The first two work out of the box with [OpenShorts Cloud](https://www.openshorts.app/)
(free tier: 20 min of video, paid plans from $12/mo). OpenShorts is also open
source: self-hosters can point either workflow at their own instance by
changing the base URL (needs a GPU box, your own Gemini key, and your own
[Upload-Post](https://www.upload-post.com/) account for the posting steps).

| Workflow | What it does | Credentials |
|---|---|---|
| `openshorts-clip-and-notify.json` | A video URL goes in through a form, clips come back on a signed webhook. No polling. | OpenShorts API key |
| `openshorts-content-machine.json` | Your YouTube channel on autopilot: daily clipping, approval buttons in Telegram, drip-scheduled posting, weekly analytics digest. | OpenShorts API key + Telegram bot |
| `openshorts-telegram-drive-youtube.json` | Self-host: a clip approved in the OpenShorts Telegram bot goes to Google Drive, then YouTube ([setup](#self-host-telegram-approval--drive--youtube)). | Google Drive + YouTube OAuth |

## Shared setup

1. In n8n: **Workflows → Import from file** → pick the workflow.
2. Create a **Header Auth** credential named `OpenShorts API key`:
   - Name: `Authorization`
   - Value: `Bearer osk_...` (create the key in your account page at
     [openshorts.app](https://www.openshorts.app/))
3. Open the **Clips ready (webhook)** node, copy its production URL, and paste
   it as `webhook_url` inside the **Start OpenShorts job** node body.
4. Optional: change `webhook_secret`. OpenShorts signs the webhook body with
   HMAC-SHA256 and sends it as `X-OpenShorts-Signature: sha256=<hex>`; verify
   it in a Code node with:

   ```javascript
   const crypto = require('crypto');
   const expected = 'sha256=' + crypto
     .createHmac('sha256', 'change-me')
     .update(JSON.stringify($json.body))
     .digest('hex');
   ```

## The Content Machine

`openshorts-content-machine.json` runs your channel in four acts:

1. **Watch** — once a day it reads your channel's RSS feed
   (`https://www.youtube.com/feeds/videos.xml?channel_id=UC...`) and clips
   **one** video: the newest upload if there is one, otherwise the next
   unprocessed video from your back catalog. One video a day keeps quota burn
   predictable; a 402 (out of minutes) pauses the machine and tells you on
   Telegram instead of failing silently.
2. **Approve from your phone** — every finished clip lands in Telegram with
   ✅ Publish / ❌ Skip buttons. Clips over Telegram's ~20 MB URL limit fall
   back to a link message with the same buttons.
3. **Drip-publish** — each approved clip takes the next free daily slot and is
   scheduled through `POST /api/social/post` to every account you connected in
   OpenShorts (TikTok, Instagram, YouTube). Approve five clips today, fill five
   days of content. The slot comes from the queue the server actually holds
   (`GET /api/social/scheduled`), so two clips approved seconds apart cannot
   book the same one.
4. **Sunday digest** — the machine reads the analytics of what it published
   (`GET /api/social/analytics/*`) and reports total impressions, per-platform
   split, and your best post of the week.

Machine-specific setup, all inside sticky notes on the canvas:

- Put your channel id in the **Channel RSS feed** node.
- Create a Telegram bot with [@BotFather](https://t.me/BotFather), add the
  `Telegram bot` credential, and replace `YOUR_TELEGRAM_CHAT_ID` in the
  notification nodes (message your bot, then check
  `api.telegram.org/bot<token>/getUpdates` for your chat id).
- Connect your social accounts in your OpenShorts account page (Cloud) — the
  posting step uses them directly; no extra social credentials in n8n.

Note: the machine remembers which videos it already processed in n8n workflow
static data, which only persists for **production** executions — test runs in
the editor won't advance it. Scheduling deliberately does *not* use that
mechanism (see act 3).

## Webhook payload

```json
{
  "event": "job.completed",
  "job_id": "…",
  "status": "completed",
  "clips": [
    { "index": 0, "title": "…", "video_url": "…", "download_url": "…" }
  ]
}
```

Failed jobs fire the same webhook with `"event": "job.failed"` and an `error`
field, so the flow never hangs waiting.

`download_url` is a 24-hour presigned link to the archived clip (hosted
service). On the self-hosted edition the same workflow runs against
`http://localhost:8000` with no API key.

## Publishing and analytics API

Direct posting: `POST /api/social/post` (accepts `scheduled_date`, ISO-8601).
Publishing queue: `GET /api/social/scheduled`, and
`DELETE /api/social/scheduled/{job_id}` to cancel one before it goes out.
Analytics of what you published: `GET /api/social/analytics` (profile totals),
`GET /api/social/analytics/posts` (per-post metrics),
`GET /api/social/analytics/impressions` (windowed totals, `period=last_week`).
Full API reference: [api.openshorts.app/docs](https://api.openshorts.app/docs).
Agent-native version of the same pipeline (MCP):
[openshorts.app/mcp](https://www.openshorts.app/mcp).

## Self-host: Telegram approval → Drive → YouTube

`openshorts-telegram-drive-youtube.json` receives a clip you approved in the
OpenShorts Telegram bot, stores it in a Google Drive folder, uploads it to
YouTube, and answers the bot with both links:

```json
{ "ok": true, "drive_file_id": "…", "drive_link": "…", "youtube_video_id": "…", "youtube_url": "…" }
```

Any failure answers `{ "ok": false, "error": "<short reason>" }`: 401 for a
wrong secret, 400 for a bad field, 200 when Drive or YouTube refuses the upload.

### 1. Start n8n

```bash
cd examples/n8n/selfhost
cp .env.example .env
python -c "import secrets;print(secrets.token_urlsafe(32))"   # paste as OPENSHORTS_WEBHOOK_SECRET
docker compose up -d
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5678/healthz   # 200
```

n8n listens on `127.0.0.1:5678` only. Keep it that way. To use the UI from
another device, either:

- open an SSH tunnel, then browse to `http://localhost:5678`:
  `ssh -L 5678:127.0.0.1:5678 <you>@<server>`
- or publish it to your tailnet only with `tailscale serve --bg 5678`, which
  proxies to the loopback port without opening it to the network.

Do the Google sign-in steps below through the SSH tunnel: n8n's OAuth
callback is `http://localhost:5678/...`, which Google accepts without a
public domain.

Import the workflow (**Workflows → Import from file**, or
`docker compose exec n8n n8n import:workflow --input=<file>` after copying it
into the container).

### 2. Google Cloud (free)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project.
2. **APIs & Services → Library**: enable **YouTube Data API v3** and
   **Google Drive API**.
3. **OAuth consent screen**: user type **External**, publishing status
   **Testing**, and add your own Google account under **Test users**.
4. **Credentials → Create credentials → OAuth client ID**, type **Web
   application**. Under **Authorized redirect URIs**, paste the
   **OAuth Redirect URL** that n8n shows in the credential dialog (step 3
   below), normally `http://localhost:5678/rest/oauth2-credential/callback`.
   Keep the client id and secret.

### 3. n8n credentials

Create two credentials with these exact names (the workflow refers to them by
name), each with the client id and secret above, then **Sign in with Google**:

- **Google Drive account** (type *Google Drive OAuth2 API*)
- **YouTube account** (type *YouTube OAuth2 API*)

Open **Upload to Drive** and **Upload to YouTube** once and check the
credential is selected.

### 4. Folder, activation, OpenShorts

1. In **Upload to Drive**, replace `PASTE_YOUR_DRIVE_FOLDER_ID_HERE` with the
   folder id (what follows `/folders/` in the folder's URL). Files are named
   `<job_id>_<clip_index>.mp4`.
2. Activate (publish) the workflow.
3. In OpenShorts' `.env`:

   ```
   N8N_WEBHOOK_SECRET=<the same value as OPENSHORTS_WEBHOOK_SECRET>
   N8N_UPLOAD_WEBHOOK_URL=http://127.0.0.1:5678/webhook/openshorts-youtube-upload
   ```

   If OpenShorts itself runs in Docker, `127.0.0.1` is its own container: use
   an address that container can reach instead.

The secret travels in the `X-OpenShorts-Secret` header and is compared in
constant time; the `Check secret and validate` node reads it from n8n's
environment (hence `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` in the compose file).

### YouTube limits

- **Quota**: an upload (`videos.insert`) costs 1,600 of the default 10,000
  daily units, so about **6 uploads a day**
  ([quota costs](https://developers.google.com/youtube/v3/determine_quota_cost)).
  A higher quota needs an extension request to Google.
- **Private until audited**: videos uploaded through an API project that has
  not passed YouTube's API compliance audit are locked to **private**, whatever
  `privacy` says. The audit is free; see the note on
  [`videos.insert`](https://developers.google.com/youtube/v3/docs/videos/insert)
  and the [audit and quota extension form](https://support.google.com/youtube/contact/yt_api_form).
- While the consent screen stays in **Testing**, Google expires the refresh
  token after 7 days: reconnect the two credentials when uploads start failing
  with an auth error.
