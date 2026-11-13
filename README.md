# Auto Shorts Agent

A small server program that turns licence-safe long videos into YouTube Shorts on its own:
it finds public-domain US government footage (NASA, official House/Senate committee feeds)
and CC BY YouTube videos, cuts the best moments into captioned vertical clips, uploads them on
a schedule, reads the analytics, adjusts its strategy once a week, and phones you for
anything that needs a human decision.

Claude is only used for judgment (Haiku scores transcript windows, Sonnet picks the exact
cut and writes the title, Sonnet reviews the week). Everything else - downloading,
transcription, editing, uploading - is plain code and costs no tokens.

## How it works

```
scout (06:10)  NASA Image Library + YouTube searches  ->  sources (candidate)
process (:20)  licence gate -> download -> whisper transcript -> Haiku scores ~60 s windows
               -> one Sonnet call picks cuts + title/hook -> ffmpeg 1080x1920 render with
                  word captions, hook text and an always-visible source credit
upload (:40)   YouTube upload as private with a scheduled publish time (posting slots)
analytics      07:25 daily: views, retention, likes, claims/takedowns
summary        20:55 phone call; weekly strategy review Sunday 18:05
approvals      major strategy changes wait for your keypad "1" (or SMS "YES <id>")
```

Files: `main.py` (scheduler + CLI), `server.py` (web: Twilio webhooks + `/dashboard`),
one module per stage (`scout fetch transcribe pick edit upload analytics strategist notify`),
`strategy.json` (what to make; rewritten weekly), `data/` (SQLite DB, token, media).

## Guardrails

* **Licence gate** (`fetch.check_licence`): government footage only from allowlisted official
  publishers (C-SPAN and news re-uploads are always rejected), CC BY only (no NC/ND/SA),
  descriptions mentioning copyright or licensed music are rejected - checked *before* anything
  is downloaded. Every clip stores the source URL, licence and evidence, and credits the
  source on screen and in the description.
* **LLM budget**: hard daily cap (`strategy.json` `daily_llm_budget_usd`, never above
  `MAX_DAILY_LLM_BUDGET_USD`). When it runs out, judgment stops until tomorrow (rendering and
  uploading continue) and you get a call. Text `PAUSE` to stop all AI spending, `RESUME` to restart.
* **Upload cap**: at most `min(uploads_per_day, MAX_UPLOADS_PER_DAY)` per day, and uploads stop
  before the YouTube API quota (10,000 units/day, 1,600 per upload) runs out.
* **Approvals**: changing the niche, adding a topic area or source type, raising the budget,
  or any change to the publisher allowlist/blocklist needs your approval by phone, SMS,
  dashboard or `python main.py approve <id>`. Unanswered requests expire after 48 h.
* **DRY_RUN** (default on): nothing is uploaded, called or texted; everything is recorded.
* **Quiet hours** (22:00-08:00): only urgent calls ring (budget cap, YouTube login or
  copyright problems); everything else waits for the morning.

## Install on a fresh Ubuntu VPS (22.04/24.04)

A 4 vCPU / 8 GB box with 40+ GB disk is comfortable (whisper and ffmpeg are CPU-heavy).

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg fontconfig fonts-dejavu-core git sqlite3
# Python must be >= 3.12 (Ubuntu 24.04 ships 3.12; on 22.04 use the deadsnakes PPA)
git clone <your repo url> /opt/clipagent && cd /opt/clipagent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env
.venv/bin/python main.py init
.venv/bin/python main.py doctor
```

**Caption font.** The shipped strategy asks for *Montserrat ExtraBold*. If it is not
installed, captions fall back automatically (Montserrat, Anton, Inter Black, ... DejaVu Sans)
and `doctor` tells you which font is used. To get the intended look, put the TTF files in
`assets/fonts/` (create the folder; e.g. `Montserrat-ExtraBold.ttf` from Google Fonts) - ffmpeg
reads fonts from there - or install the family system-wide (`fonts-montserrat` on Debian/Ubuntu
if your release has it).

## Configuration

Everything lives in `config.env` (documented line by line in `config.env.example`):

1. **Anthropic**: `ANTHROPIC_API_KEY`, then `FAKE_LLM=false`.
2. **YouTube**: create the OAuth client and authorise once - follow
   [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md) (`.venv/bin/python youtube_auth.py`, then
   `--check`). Optional: `YOUTUBE_MONETARY_ANALYTICS=true` with `youtube_auth.py --monetary`
   to record revenue.
3. **Twilio + your phone**: follow [docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md)
   (`TWILIO_*`, `OWNER_PHONE`).
4. **HTTPS**: Twilio and the dashboard need a public HTTPS address - see
   [deploy/README.md](deploy/README.md) (Caddy). Set `PUBLIC_BASE_URL` and `DASHBOARD_TOKEN`.
5. **Run as a service**: `deploy/clipagent.service` (systemd, restarts on failure).

What gets made is controlled by `strategy.json` (topics, allowlist, caption style, clip length,
posting times, budget, the running A/B experiment). The weekly review edits it for you; if you
edit it by hand, `python main.py doctor` validates it.

## First run: DRY_RUN + FAKE_LLM

With the defaults (`DRY_RUN=true`, `FAKE_LLM=true`) nothing leaves the box except downloads:

```bash
.venv/bin/python main.py manual "https://www.youtube.com/watch?v=<a NASA video>"
.venv/bin/python main.py status
ls data/media/renders/          # watch the .mp4 files
```

Note: the built-in fake LLM scores every window 5/10, below `min_haiku_score` (7), so with
`FAKE_LLM=true` sources usually end with "nothing picked". That proves fetch/transcribe/licence
work; set a real `ANTHROPIC_API_KEY` and `FAKE_LLM=false` (budget-capped) to get clips.

## Go-live checklist (staged)

Do these in order; each step leaves `DRY_RUN=true` until step 3.

1. **Manual pipeline.** `doctor` has no FAIL lines. With a real API key, run
   `main.py manual <url> --no-upload` on 2-3 NASA / committee videos. Watch the renders on a
   phone: captions readable, hook in the first 3 s, credit visible, cut starts and ends on a
   sentence.
2. **Picker.** `main.py process` on a few scouted sources (`main.py scout` first). Check
   `status` spend per source (expect a few cents) and the dashboard's clip list for quality.
3. **Upload.** Authorise YouTube, set `DRY_RUN=false`, then
   `main.py requeue-dry-run` (makes clips that were only "uploaded" in dry run eligible again)
   and `main.py upload`. Uploads land as *private* with a scheduled publish time; confirm in
   YouTube Studio that they go public on time. Unaudited API projects lock uploads to private
   (you get a text) - submit Google's audit form.
4. **Notifications.** Configure Twilio + HTTPS, run `main.py summary` (you should get a call)
   and text `STATUS` to your Twilio number.
5. **Analytics.** After 2-3 days: `main.py analytics`; the dashboard shows views and retention.
6. **Strategist.** After a week with views: `main.py review`. Minor changes are applied, major
   ones phone you for approval.
7. **Scout / autopilot.** `sudo systemctl enable --now clipagent` - the scheduler now runs
   everything on its own. Check `status` and the dashboard daily for the first week.

## Day-to-day commands

```bash
.venv/bin/python main.py status                 # one-screen summary
.venv/bin/python main.py doctor [--network]     # preflight / troubleshooting
.venv/bin/python main.py manual <url> [--source-type nasa|congress|cc_by_youtube] [--no-upload]
.venv/bin/python main.py run-once               # scout + process + upload now
.venv/bin/python main.py scout | process [--limit N] | upload [--limit N]
.venv/bin/python main.py analytics [--day YYYY-MM-DD] | review | summary
.venv/bin/python main.py approve <id> | reject <id>
.venv/bin/python main.py cleanup                # delete old media now (also daily at 03:35)
.venv/bin/python main.py requeue-dry-run        # once, when switching DRY_RUN off
.venv/bin/python main.py schedule | serve       # scheduler + web / web only (systemd uses schedule)
```

SMS commands to your Twilio number: `YES <id>`, `NO <id>`, `STATUS`, `PAUSE`, `RESUME`, `HELP`.
Dashboard: `https://<your host>/dashboard/`.

Failed sources are retried automatically twice (an hour apart), failed renders twice; after
that they stay `failed` with the reason shown on the dashboard. Disk: source videos are
deleted 3 days after a source is finished, renders 14 days after their upload
(`SOURCE_MEDIA_RETENTION_DAYS`, `RENDER_RETENTION_DAYS`); the database, licence evidence
(`info.json`) and transcripts are kept.

## Costs (rough, at list prices)

| Item | Estimate |
|---|---|
| Haiku window scoring | ~$0.0002 per window, at most 40 windows per source (`max_windows_per_source`) |
| Sonnet cut + metadata | ~$0.02-0.04 per source |
| Weekly review | ~$0.05-0.10 |
| Typical day (3-6 sources) | $0.10-0.30, hard-capped at `daily_llm_budget_usd` (default $1) |
| Twilio | number ~$1.15/month, calls ~$0.014/min, SMS ~$0.008 each (US) |
| VPS | ~$20-40/month for 4 vCPU / 8 GB |

Exact spend per call is in the dashboard's costs page and `status`.

## Troubleshooting

* **Start with `main.py doctor`** (add `--network` on the server): every FAIL line says how to fix it.
* **Nothing is uploaded**: `status` shows the daily cap, quota and DRY_RUN state; clips must be
  `rendered`. In live mode a missing/expired YouTube token stops uploads and calls you - re-run
  `youtube_auth.py`.
* **Sources keep getting rejected**: the reason and the evidence are on the dashboard's
  sources page; the allowlist is in `strategy.json`.
* **"busy"** from a CLI command: the scheduler is running that stage right now; try again later.
* **Budget stopped early**: text `STATUS`; spending resumes at local midnight (or `RESUME` if
  you paused it).
* **Logs**: `journalctl -u clipagent -f`; operator events are on the dashboard's events page.

## Not yet verified against live services

Everything below is implemented and covered by offline tests with fakes, but has **not** been
exercised against the real service. Watch these closely during the go-live steps:

* **yt-dlp downloads** (real YouTube / NASA / house.gov URLs, format selection, merge to mp4)
  and the exact `license` / `channel_id` strings yt-dlp reports for CC BY and official channels.
* **YouTube Creative Commons search** via yt-dlp with the results-page filter
  (`sp=EgIwAQ%3D%3D`), and the alternative Data API backend.
* **NASA Image and Video Library API** response shapes (`/search`, `/asset/{id}`,
  `metadata.json` duration fields).
* **faster-whisper** transcription quality/speed on your VPS and the first model download.
* **Anthropic API**: real Haiku/Sonnet calls (model ids `claude-haiku-5-5`,
  `claude-sonnet-5-5`, structured output, prompt caching, the Sonnet server-side fallback
  beta) were only tested against an injected fake client.
* **YouTube Data API uploads** (resumable upload, `publishAt` scheduling, quota error handling,
  crash recovery by marker tag) and the private-lock behaviour of unaudited projects.
* **YouTube Analytics** report shapes (per-video daily query and its fallback, retention curve,
  revenue metrics) and the indirect takedown/claim signals in `check_claims`.
* **Twilio**: real calls, keypad gather, answering-machine detection, SMS replies and webhook
  signature validation behind your reverse proxy.
* **Deployment**: the systemd unit, Caddy config and backup cron in `deploy/` were written,
  not run; `doctor --network` was not run from a VPS.
* **Captions with Montserrat**: renders were checked with the Inter Black fallback only.
