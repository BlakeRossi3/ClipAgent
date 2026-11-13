"""YouTube OAuth + shared API plumbing (credentials, service builders, quota tally).

One OAuth "Desktop app" client and one refresh token cover everything the agent
does on YouTube:

* ``youtube.upload``           - ``videos.insert`` (upload.py)
* ``youtube.readonly``         - ``videos.list`` / ``channels.list`` / ``playlistItems.list``
  on our own (incl. private) videos: status checks, lifetime counters (analytics.py)
* ``yt-analytics.readonly``    - YouTube Analytics API v2 ``reports.query``
* ``yt-analytics-monetary.readonly`` - *optional*, only when authorised with
  ``--monetary`` (revenue metrics). There is no settings flag for it; whether the
  saved token has it is checked at call time with :func:`has_scope`.

One-time authorisation (run on the server, see docs/YOUTUBE_SETUP.md)::

    python youtube_auth.py                 # loopback server on :8080 (tunnel it over SSH)
    python youtube_auth.py --port 8765     # pick another port
    python youtube_auth.py --manual        # no tunnel: paste the redirected URL back
    python youtube_auth.py --check         # load/refresh the saved token, print the channel

The token is written to ``settings.youtube_token_path`` with mode 0600. At run
time :func:`load_credentials` refreshes it automatically; when it is missing,
revoked or expired it raises :class:`YouTubeAuthError` *and* records an error
event plus a queued ``youtube_auth`` call notification (once per local day) so
``notify.send_queued()`` phones the owner.

Quota: the Data API v3 bills "units" per call against a project-wide daily quota
(default 10,000) that resets at **midnight Pacific time**. :data:`QUOTA_COSTS`
is the cost table and :func:`record_quota` keeps a local tally in ``kv`` keyed
by the Pacific date, so uploads can stop *before* the quota runs out. The YouTube
Analytics API has its own separate quota and is not tallied here.

No module-level side effects; googleapiclient / google-auth are imported lazily.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import db
from config import Settings, get_settings

log = logging.getLogger(__name__)

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_MONETARY = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"

SCOPES: tuple[str, ...] = (SCOPE_UPLOAD, SCOPE_READONLY, SCOPE_ANALYTICS)
MONETARY_SCOPES: tuple[str, ...] = SCOPES + (SCOPE_MONETARY,)

# --- Data API v3 quota ------------------------------------------------------
# Units per call (https://developers.google.com/youtube/v3/determine_quota_cost).
# Keep this table the single place to update when Google changes prices.
QUOTA_COSTS: dict[str, int] = {
    "videos.insert": db.UPLOAD_QUOTA_UNITS,   # 1600
    "videos.list": 1,
    "videos.update": 50,
    "channels.list": 1,
    "playlistItems.list": 1,
    "search.list": 100,
    "thumbnails.set": 50,
}
DEFAULT_DAILY_QUOTA = 10_000
# Units kept free for the day's list/status/analytics-adjacent Data API calls.
QUOTA_RESERVE = 200
QUOTA_TZ = ZoneInfo("America/Los_Angeles")   # Data API daily quota resets at midnight PT
_QUOTA_KEY = "youtube:quota:{day}"            # int units used on a Pacific date
_QUOTA_LIMIT_KEY = "youtube:quota_limit"      # optional override after a quota increase
_AUTH_ALERT_KEY = "youtube:auth_alerted:{day}"


class YouTubeAuthError(RuntimeError):
    """No usable YouTube token (missing, revoked, expired or lacking scopes)."""


# ---------------------------------------------------------------------------
# Quota tally (Pacific-day boundary)
# ---------------------------------------------------------------------------

def quota_day(now: Any = None) -> date:
    """The Pacific-time date the Data API quota is currently counting against."""
    return (now or db.utcnow()).astimezone(QUOTA_TZ).date()


def daily_quota_limit() -> int:
    """10,000 by default; ``kv['youtube:quota_limit']`` overrides it after Google grants more."""
    v = db.kv_get(_QUOTA_LIMIT_KEY)
    try:
        return int(v) if v is not None else DEFAULT_DAILY_QUOTA
    except (TypeError, ValueError):
        return DEFAULT_DAILY_QUOTA


def quota_used(day: date | None = None) -> int:
    return int(db.kv_get(_QUOTA_KEY.format(day=(day or quota_day()).isoformat()), 0) or 0)


def quota_remaining(day: date | None = None) -> int:
    return daily_quota_limit() - quota_used(day)


def record_quota(method: str, calls: int = 1, *, units: int | None = None) -> int:
    """Add the cost of ``calls`` x ``method`` to today's (Pacific) tally. Returns the new total."""
    cost = units if units is not None else QUOTA_COSTS.get(method, 1) * calls
    key = _QUOTA_KEY.format(day=quota_day().isoformat())
    with db.transaction() as c:
        c.execute("BEGIN IMMEDIATE")  # serialise read-modify-write across processes
        total = int(db.kv_get(key, 0, conn=c) or 0) + int(cost)
        db.kv_set(key, total, conn=c)
    return total


def quota_allows(method: str, *, reserve: int = QUOTA_RESERVE) -> bool:
    """True if ``method`` fits today's remaining quota while keeping ``reserve`` units free."""
    return quota_used() + QUOTA_COSTS.get(method, 1) + reserve <= daily_quota_limit()


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def token_path(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).youtube_token_path)


def has_token(settings: Settings | None = None) -> bool:
    return token_path(settings).is_file()


def save_credentials(creds: Any, path: Path | None = None) -> Path:
    """Atomically write the authorised-user JSON with 0600 permissions."""
    path = Path(path or token_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".youtube_token.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def report_auth_problem(message: str, *, error: str | None = None) -> None:
    """Error event every time; a queued ``youtube_auth`` call notification once per local day."""
    db.log_event("youtube_auth", message, level="error", data={"error": error})
    day = db.local_today().isoformat()
    if db.kv_set_if_absent(_AUTH_ALERT_KEY.format(day=day), {"message": message}):
        db.add_notification(
            "youtube_auth",
            f"YouTube access needs attention: {message} Uploads and analytics are paused until "
            f"you re-run the YouTube authorisation on the server.",
            "call")


def load_credentials(*, settings: Settings | None = None, scopes: tuple[str, ...] = SCOPES,
                     refresh: bool = True) -> Any:
    """Return valid ``google.oauth2.credentials.Credentials`` (refreshing and re-saving if needed).

    Raises :class:`YouTubeAuthError` (after :func:`report_auth_problem`) when the token
    is missing, unreadable, lacks a required scope or cannot be refreshed.
    """
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(settings)
    if not path.is_file():
        msg = f"YouTube token not found at {path}; run `python youtube_auth.py` once."
        report_auth_problem(msg)
        raise YouTubeAuthError(msg)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(info)
    except (OSError, ValueError) as exc:
        msg = f"YouTube token at {path} is unreadable; re-run `python youtube_auth.py`."
        report_auth_problem(msg, error=str(exc))
        raise YouTubeAuthError(msg) from exc

    granted = set(creds.scopes or info.get("scopes") or [])
    missing = [s for s in scopes if granted and s not in granted]
    if missing:
        msg = "YouTube token lacks scopes " + ", ".join(m.rsplit("/", 1)[-1] for m in missing) + \
              "; re-run `python youtube_auth.py`."
        report_auth_problem(msg)
        raise YouTubeAuthError(msg)

    if refresh and not creds.valid:
        if not creds.refresh_token:
            msg = "YouTube token has no refresh token; re-run `python youtube_auth.py`."
            report_auth_problem(msg)
            raise YouTubeAuthError(msg)
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # invalid_grant: revoked, password change, or a "Testing" consent screen's 7-day expiry.
            msg = "YouTube refresh token was revoked or expired; re-run `python youtube_auth.py`."
            report_auth_problem(msg, error=str(exc))
            raise YouTubeAuthError(msg) from exc
        except TransportError:
            raise  # network trouble is transient: not an auth problem, do not page the owner
        save_credentials(creds, path)
    return creds


def has_scope(scope: str, settings: Settings | None = None) -> bool:
    """Whether the saved token was granted ``scope`` (no network)."""
    try:
        info = json.loads(token_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return scope in set(info.get("scopes") or [])


def build_service(name: str, version: str, *, credentials: Any = None) -> Any:
    """``googleapiclient.discovery.build`` with the bundled (static) discovery document."""
    from googleapiclient.discovery import build

    creds = credentials if credentials is not None else load_credentials()
    return build(name, version, credentials=creds, cache_discovery=False, static_discovery=True)


def youtube_service(credentials: Any = None) -> Any:
    return build_service("youtube", "v3", credentials=credentials)


def analytics_service(credentials: Any = None) -> Any:
    return build_service("youtubeAnalytics", "v2", credentials=credentials)


# ---------------------------------------------------------------------------
# Interactive authorisation (one-time, by the owner)
# ---------------------------------------------------------------------------

def _make_flow(scopes: tuple[str, ...], settings: Settings) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = Path(settings.youtube_client_secrets)
    if not secrets.is_file():
        raise SystemExit(f"OAuth client file not found: {secrets}\n"
                         "Download the Desktop-app client JSON from Google Cloud console "
                         "(see docs/YOUTUBE_SETUP.md) and set YOUTUBE_CLIENT_SECRETS.")
    return InstalledAppFlow.from_client_secrets_file(str(secrets), scopes=list(scopes))


_AUTH_KW = {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}


def authorize_local_server(*, port: int = 8080, scopes: tuple[str, ...] = SCOPES,
                           open_browser: bool = False, settings: Settings | None = None) -> Any:
    """Loopback flow. On a headless VPS, tunnel the port from your laptop first:
    ``ssh -L 8080:localhost:8080 you@vps`` then open the printed URL locally."""
    settings = settings or get_settings()
    flow = _make_flow(scopes, settings)
    prompt = ("\nOpen this URL in a browser on a machine that can reach localhost:%d on this server\n"
              "(for a VPS: `ssh -N -L %d:localhost:%d <user>@<server>` on your laptop first):\n\n{url}\n"
              % (port, port, port))
    return flow.run_local_server(host="localhost", port=port, open_browser=open_browser,
                                 authorization_prompt_message=prompt, **_AUTH_KW)


def authorize_manual(*, port: int = 8080, scopes: tuple[str, ...] = SCOPES,
                     settings: Settings | None = None, input_fn: Any = input) -> Any:
    """Copy/paste flow (no tunnel). Google redirects your browser to ``http://localhost:<port>/?code=...``;
    that page fails to load - copy the full URL from the address bar and paste it here."""
    settings = settings or get_settings()
    flow = _make_flow(scopes, settings)
    flow.redirect_uri = f"http://localhost:{port}/"
    url, _state = flow.authorization_url(**_AUTH_KW)
    print("\n1. Open this URL in any browser and approve access:\n\n" + url)
    print("\n2. Your browser ends on a page that does not load (http://localhost...).\n"
          "   Copy the WHOLE address from the address bar and paste it below.\n")
    pasted = input_fn("Redirected URL: ").strip()
    if not pasted.startswith("http"):
        raise SystemExit("Expected the full http://localhost... URL from the address bar.")
    if "code" not in parse_qs(urlparse(pasted).query):
        raise SystemExit("That URL has no ?code= parameter (did you deny access?).")
    # oauthlib insists on https for the response URL; the loopback redirect is http by design.
    flow.fetch_token(authorization_response=pasted.replace("http://", "https://", 1))
    return flow.credentials


def _check(settings: Settings) -> int:
    creds = load_credentials(settings=settings)
    yt = youtube_service(creds)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    record_quota("channels.list")
    items = resp.get("items") or []
    if not items:
        print("Token works, but this Google account has no YouTube channel.")
        return 1
    print(f"OK: authorised for channel {items[0]['snippet']['title']!r} ({items[0]['id']}); "
          f"scopes: {', '.join(s.rsplit('/', 1)[-1] for s in (creds.scopes or []))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Authorise the Auto Shorts Agent for YouTube (one-time).")
    ap.add_argument("--port", type=int, default=8080, help="loopback port for the OAuth redirect (default 8080)")
    ap.add_argument("--manual", action="store_true", help="copy/paste the redirected URL instead of a local server")
    ap.add_argument("--open-browser", action="store_true", help="try to open a local browser (desktop machines)")
    ap.add_argument("--monetary", action="store_true", help="also request yt-analytics-monetary.readonly (revenue)")
    ap.add_argument("--check", action="store_true", help="verify the saved token (refresh + channels.list, 1 unit)")
    args = ap.parse_args(argv)
    settings = get_settings()

    if args.check:
        try:
            return _check(settings)
        except YouTubeAuthError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2

    scopes = MONETARY_SCOPES if args.monetary else SCOPES
    if args.manual:
        creds = authorize_manual(port=args.port, scopes=scopes, settings=settings)
    else:
        creds = authorize_local_server(port=args.port, scopes=scopes, open_browser=args.open_browser,
                                       settings=settings)
    if not creds.refresh_token:
        print("WARNING: Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
    path = save_credentials(creds, token_path(settings))
    db.kv_delete(_AUTH_ALERT_KEY.format(day=db.local_today().isoformat()))
    db.log_event("youtube_auth", "YouTube authorised", data={"scopes": list(creds.scopes or scopes)})
    print(f"Saved token to {path} (mode 0600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
