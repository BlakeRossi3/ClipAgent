# YouTube setup (one-time, ~20 minutes)

The agent uploads Shorts with the **YouTube Data API v3** and reads performance with the
**YouTube Analytics API**. Both use one Google OAuth client and one saved refresh token.
Items marked **(verify)** are believed correct but Google changes these screens and
policies often; check them against the current console.

## Read this first: uploads stay private until Google audits your API project

Google's rule: videos uploaded with `videos.insert` by an **API project that has not passed
the YouTube API Services compliance audit** are locked to **private**, regardless of the
privacy or `publishAt` the agent asks for. So until your project is audited:

* the agent can still upload every Short (privately, scheduled for its slot), but
* **nothing goes public automatically**. Open YouTube Studio and publish them by hand.
  `analytics.check_claims()` notices scheduled Shorts that are still private after their
  slot and texts you once a day.

To lift the lock, submit the **YouTube API Services - Audit and Quota Extension Form**
(linked from the YouTube Data API "Quota and compliance audits" page) for this project. You
describe the app (a private tool that uploads to your own channel), and Google reviews it
against the YouTube API Services Terms and Developer Policies. Reviews can take weeks.
The same form is how you ask for more quota.

## 1. Google Cloud project

1. Go to <https://console.cloud.google.com/>, create a project (for example `auto-shorts-agent`).
   Sign in with the Google account that owns the YouTube channel, or one that can manage it.
2. **APIs & Services > Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.

## 2. OAuth consent screen

In **APIs & Services > OAuth consent screen** (newer consoles call it *Google Auth Platform*,
with *Branding*, *Audience* and *Data access* pages) **(verify)**:

1. User type **External**. Give it an app name, a support email and a developer email.
2. Scopes (*Data access*): add
   * `https://www.googleapis.com/auth/youtube.upload`
   * `https://www.googleapis.com/auth/youtube.readonly`
   * `https://www.googleapis.com/auth/yt-analytics.readonly`
   * optionally `https://www.googleapis.com/auth/yt-analytics-monetary.readonly` (revenue, only if
     you authorise with `--monetary`)
3. **Test users**: add the channel owner's Google account.
4. **Publishing status matters.** While the app is in **Testing**, Google issues refresh tokens
   that **expire after 7 days**. The agent would lose access every week and phone you about it.
   For a long-running agent, click **Publish app** (status *In production*). For a personal app
   that is not verified, you get a "Google hasn't verified this app" warning when you authorise.
   Click *Advanced > Go to (app name)* to continue. Unverified production apps are limited
   to a small number of users, which is fine here **(verify)**. This OAuth app verification
   is separate from the YouTube API audit above.

## 3. OAuth client

1. **APIs & Services > Credentials > Create credentials > OAuth client ID**, type **Desktop app**.
2. Download the JSON and copy it to the server as `client_secret.json` in the repo root (or set
   `YOUTUBE_CLIENT_SECRETS=/path/to/file.json` in `config.env`). Keep it out of git.

## 4. Authorise (run once on the server)

The token is saved to `YOUTUBE_TOKEN_PATH` (default `data/youtube_token.json`, mode 0600) and
refreshed automatically after that.

**Option A: SSH tunnel (recommended).** On your laptop:

```bash
ssh -N -L 8080:localhost:8080 you@your-server       # leave this running
```

On the server:

```bash
.venv/bin/python youtube_auth.py                     # --port 8765 to use another port
```

Open the printed URL in your laptop browser and approve. Google redirects to
`http://localhost:8080/`, which the tunnel carries to the server, and the script saves the token.

**Option B: copy and paste (no tunnel).**

```bash
.venv/bin/python youtube_auth.py --manual
```

Open the URL anywhere and approve. The browser lands on a `http://localhost:8080/?code=...`
page that does not load. Copy the whole address from the address bar and paste it into the
terminal.

**Check it:** `.venv/bin/python youtube_auth.py --check` refreshes the token and prints the
channel name. This costs 1 quota unit.

If Google returns no refresh token (it can happen when you re-authorise), remove the app at
<https://myaccount.google.com/permissions> and run the script again. The script always requests
offline access with `prompt=consent`.

When the token is missing, revoked or expired, uploads and analytics stop. The agent then
logs an error event and queues a `youtube_auth` phone call to you, at most once a day.
Re-run step 4 to fix it.

## 5. Go live

In `config.env` set `DRY_RUN=false`. Optionally set `YOUTUBE_DEFAULT_PRIVACY` (used only for
immediate, unscheduled uploads; scheduled uploads are always `private` + `publishAt`) and
`YOUTUBE_CATEGORY_ID` (default 28, Science & Technology).

## Quota math

* Default Data API quota: **10,000 units per day per project**, reset at **midnight Pacific time**.
* `videos.insert` costs **1,600 units** (`youtube_auth.QUOTA_COSTS`, update it there if Google
  changes the price **(verify)** against the current quota calculator). `videos.list`,
  `channels.list` and `playlistItems.list` cost 1 unit each.
* 6 uploads = 9,600 units, which leaves 400 for status and statistics calls. So
  `MAX_UPLOADS_PER_DAY` must stay **<= 6** on the default quota. The agent keeps a local
  tally per Pacific day and refuses an upload that would leave fewer than 200 units
  (`QUOTA_RESERVE`). If YouTube still answers `quotaExceeded` or `uploadLimitExceeded`, uploads
  stop until the next Pacific midnight and you get a text.
* The YouTube Analytics API has its own quota. The daily pull uses a few dozen requests and
  does not touch the Data API units.
* **More quota:** use the same Audit and Quota Extension Form. Once Google grants it, set
  `YOUTUBE_DAILY_QUOTA=50000` (your new limit) in `config.env`, restart the service, and raise
  `MAX_UPLOADS_PER_DAY` / `uploads_per_day` to match.
* There is also a per-channel upload limit, separate from API quota and not published by
  Google. Hitting it returns `uploadLimitExceeded`, which is handled as above.

## What the agent sets on every upload

* Title: at most 100 characters, cut on a word boundary, always ends with `#Shorts`. `<` and `>`
  are removed because the API rejects them.
* Description: at most 5,000 bytes. It **always** includes the credit line, the source URL and
  the licence. For CC BY sources it also names the original title and author, links the licence
  (YouTube's CC option is CC BY 3.0) and states what was changed. If space runs out, the
  creative text is shortened, never the credits.
* `selfDeclaredMadeForKids: false`, `categoryId` from settings, tags from the hashtags, and a
  hidden marker tag `clipagent-clip-<id>` that the agent uses to recover from a crash mid-upload
  without uploading twice.
* **Licence field = Standard YouTube licence**, on purpose. The source is either public domain,
  where putting a CC licence on it would claim rights nobody holds, or CC BY, which requires
  attribution but not a CC licence on the derivative. Change it by hand in Studio if you want
  your edits to be CC BY.
* Vertical (or square) video of at most 3 minutes, which YouTube treats as a Short.

## Copyright claims: what the agent can and cannot see

Content ID claim details (who claimed, monetisation or block policy) are **not available to
ordinary channels through the API**. Only content owners using the partner APIs can see them.
`check_claims()` relies on indirect signals: `uploadStatus` rejected, failed or deleted (with
`rejectionReason`, for example `claim` or `copyright`), regional blocking in
`contentDetails.regionRestriction`, and videos that disappeared from the channel. Each one
queues an urgent call. **Glance at YouTube Studio > Content > Copyright now and then**:
monetisation-only claims do not show up in any API signal.
