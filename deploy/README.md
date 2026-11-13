# Deploying on a VPS

One process (`python main.py schedule`) runs the scheduler and the web server
(Twilio webhooks + dashboard) on `127.0.0.1:8000`. A reverse proxy adds HTTPS.

## systemd

```bash
sudo useradd --system --create-home --home-dir /opt/clipagent clipagent   # or reuse an existing user
sudo -u clipagent git clone <repo> /opt/clipagent                        # then install per ../README.md
sudo cp /opt/clipagent/deploy/clipagent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now clipagent
journalctl -u clipagent -f            # logs
sudo systemctl restart clipagent      # after editing config.env
```

Edit `User=`, `WorkingDirectory=` and the paths in the unit if you installed elsewhere.
A lock file (`data/scheduler.lock`) stops a second scheduler on the same data dir, so do not
also run `main.py schedule` by hand while the service is up (one-off commands such as
`status`, `manual` or `doctor` are fine; pipeline commands wait their turn via per-stage locks
and report `busy` if a job holds them).

The unit sets `HF_HOME` so the whisper model is cached under `data/hf-cache`. If you ran
`doctor`/`manual` by hand as another user first, run them with the same `HF_HOME`.

## HTTPS reverse proxy (Caddy)

Twilio must reach `/twilio/*` over HTTPS, and the dashboard should never be served over plain
HTTP. Caddy obtains certificates automatically:

```bash
sudo apt install caddy
```

`/etc/caddy/Caddyfile`:

```
agent.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
```

Then set in `config.env`:

```
PUBLIC_BASE_URL=https://agent.example.com
DASHBOARD_TOKEN=<long random string>     # openssl rand -hex 24
```

`PUBLIC_BASE_URL` must be exactly the URL Twilio calls: webhook signatures are verified against
it. Open the dashboard at `https://agent.example.com/dashboard/` and log in with the token.
Check `https://agent.example.com/healthz` returns `{"status": "ok", ...}`.

Firewall: allow 22, 80 (certificate issuance) and 443 only; port 8000 stays on localhost.

## Backups

Everything worth keeping lives in `data/` plus two files in the repo root:

| What | Where | Why |
|---|---|---|
| database | `data/clipagent.db` (+ `-wal`, `-shm`) | sources, clips, licence evidence, uploads, stats, decisions |
| YouTube token | `data/youtube_token.json` | avoids re-authorising |
| licence evidence + transcripts | `data/media/downloads/*/info.json`, `data/media/transcripts/` | audit trail; small |
| config | `config.env`, `client_secret.json` | secrets |
| strategy | `strategy.json` (history is also in the DB) | live strategy |

Source videos and renders are large and re-creatable; the daily cleanup job deletes them
anyway (`SOURCE_MEDIA_RETENTION_DAYS`, `RENDER_RETENTION_DAYS`).

Nightly backup (consistent even while the service runs, thanks to SQLite's online backup):

```bash
# /etc/cron.d/clipagent-backup
15 4 * * * clipagent cd /opt/clipagent && mkdir -p backups && \
  sqlite3 data/clipagent.db ".backup backups/clipagent-$(date +\%F).db" && \
  tar czf backups/meta-$(date +\%F).tgz config.env client_secret.json strategy.json \
      data/youtube_token.json data/media/transcripts $(ls -d data/media/downloads/*/info.json 2>/dev/null) && \
  find backups -mtime +14 -delete
```

(`sudo apt install sqlite3` for the CLI.) Copy `backups/` off the box (rsync, restic, the
provider's snapshot feature) - a backup on the same disk does not survive losing the VPS.
The backups contain secrets: keep them private.

Restore: stop the service, copy the `.db` back to `data/clipagent.db` (delete stale
`-wal`/`-shm`), unpack the tarball, start the service, run `python main.py doctor`.
