# Auto Shorts Agent - engineering contract

Autonomous agent that clips **licence-safe** footage (US federal government works:
NASA, official House/Senate committee feeds, DVIDS, NARA; and **CC BY** YouTube
videos) into YouTube Shorts, learns from analytics, and phones the owner (Blake)
for updates and approvals.

Principles: plain code does the heavy lifting (download, transcribe, cut,
caption, upload: zero LLM tokens). Claude is used only for judgment: **Haiku**
scores transcript windows; **Sonnet** picks exact cuts + title/description/hashtags
and runs the weekly strategy review. `strategy.json` drives all daily behaviour.
Guardrails: hard daily LLM budget, upload cap under the YouTube quota, phone
approval before any major change.

## Layout (flat modules at repo root - keep it flat)

| File | Role | Status |
|---|---|---|
| `config.py` | typed `Settings` from env + `config.env` | **done** |
| `db.py` | SQLite schema/migrations, time helpers, typed table helpers | **done** |
| `strategy.py` + `strategy.json` | strategy schema, validation, versioned saves, major-change diff, publisher allowlist | **done** |
| `budget.py` | pricing, usage recording, daily cap | **done** |
| `llm.py` | the ONLY Anthropic SDK user; fake mode | **done** |
| `scout.py` | find candidate sources -> `sources` | **done** |
| `fetch.py` | yt-dlp download + licence check | **done** |
| `transcribe.py` | faster-whisper, word timestamps | **done** |
| `pick.py` | windows -> Haiku scores -> Sonnet cuts -> `clips` | **done** |
| `edit.py` | ffmpeg 9:16 render, captions, hook, credit | **done** |
| `upload.py` | YouTube Data API v3 upload + scheduling | **done** |
| `youtube_auth.py` | OAuth token, service builders, Data API quota tally | **done** |
| `analytics.py` | daily YouTube Analytics pull -> `stats` | **done** |
| `strategist.py` | weekly Sonnet review -> new strategy | **done** |
| `notify.py` | Twilio voice/SMS + webhook `router` | **done** |
| `dashboard.py` | FastAPI `router` + `templates/` | **done** |
| `server.py` | FastAPI `app` (notify + dashboard routers) | **done** |
| `main.py` | APScheduler jobs + CLI; portable locks (fcntl / msvcrt), Restart/Stop control | **done** |
| `config_store.py` | comment-preserving atomic edits of `config.env` (wizard writes) | **done** |
| `setup_web.py` | `/setup` wizard + Settings page (forms, YouTube OAuth, vault import, update/restart/stop) | **done** |
| `voice/` | always-on local voice loop (`python -m voice`; wake word, STT, TTS, barge-in, mute) - docs/VOICE.md | **done** |
| `windows/` | `ClipAgent-Setup.bat` + `setup.ps1` installer, `ClipAgent.ps1` launcher (docs/WINDOWS.md) | |
| `tests/` | pytest; `conftest.py` fixtures are shared; `test_e2e.py` runs the real pipeline with ffmpeg | |
| `deploy/` | systemd unit, reverse proxy + backup notes | |

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp config.env.example config.env   # then fill in; config.env is gitignored
.venv/bin/python -m pytest -q      # all tests offline, zero tokens
```
Python >= 3.12. External binaries used at runtime: `ffmpeg`/`ffprobe` (with libass + libx264), `fc-list`.
Owner-facing docs: `README.md` (install, go-live checklist, unverified-live list), `deploy/README.md`.

## Conventions (mandatory)

- **Timestamps**: UTC ISO-8601 text `YYYY-MM-DDTHH:MM:SSZ` everywhere in the DB
  (`db.now_iso()`, `db.to_iso(dt)`, `db.parse_iso(s)`). Python datetimes are
  timezone-aware. Never call `datetime.now()` directly in pipeline code - use
  `db.utcnow()` (tests freeze it via the `clock` fixture).
- **"Today"** = local date in `TIMEZONE` (default America/New_York):
  `db.local_today()`, `db.local_day_bounds(day) -> (start_iso, end_iso)` (UTC, half-open).
  Daily budget, upload cap and posting slots all use local days. `stats.day` and
  `channel_stats.day` are local `YYYY-MM-DD`.
- **Statuses** are state machines enforced by `db.set_source_status` / `db.set_clip_status`
  (see `db.SOURCE_TRANSITIONS`, `db.CLIP_TRANSITIONS`; `force=True` only for manual repair):
  - source: `candidate -> fetched -> transcribed -> picked -> done`; any stage -> `rejected` (licence/allowlist, with reason) or `failed` (with reason); `transcribed -> done` when nothing worth clipping; `failed -> candidate|fetched|transcribed` to retry; `rejected -> candidate` = manual re-approval.
  - clip: `picked -> rendered -> [scheduled] -> uploaded`, plus `failed` / `skipped`. `scheduled` = rendered and holding a publish slot but not yet sent to YouTube (optional).
  - upload: `queued -> uploaded | failed`, `removed` (taken down), `superseded` (a dry-run upload replaced via `main.py requeue-dry-run`; its fake id is cleared).
  - decision: `pending -> approved | rejected | expired` (first answer wins).
- **No raw SQL outside `db.py`** when a helper exists. If you need a new query,
  add a typed helper to `db.py` (with a test) rather than inlining SQL. Schema
  changes = append a new migration string to `db.MIGRATIONS`; never edit a shipped one.
- **No module-level side effects**: importing any module must not touch the
  network, filesystem, DB or env. Read settings with `config.get_settings()` at
  call time. Heavy/optional imports (`faster_whisper`, `googleapiclient`,
  `twilio`, `yt_dlp`) go inside functions.
- **DRY_RUN** (default `true`): no upload/call/SMS reaches an external service.
  Record what *would* happen (uploads get `dry_run=1` and a fake video id like
  `dryrun-<clip_id>`, quota 0; notifications get status `dry_run`). Downloads,
  transcription and rendering still run locally in dry-run mode.
- **FAKE_LLM** / `llm.set_fake_handler(fn)`: `llm.complete` never calls Anthropic.
- **LLM access only via `llm.complete`**. Never import `anthropic` elsewhere.
  `llm` enforces the budget and records usage; callers handle `budget.BudgetExceeded`
  by stopping LLM work for the day and leaving items in their current status (do not mark them failed).
- **Logging**: `db.log_event(module, message, level="info"|"warning"|"error", data=...)`
  for anything an operator would want on the dashboard (stage completed, rejection,
  failure, upload, approval). Use `logging.getLogger(__name__)` for debug chatter.
  Only `main.py` configures logging handlers.
- **Errors**: a stage function that fails sets the row to `failed` with a reason,
  logs an `error` event, then raises its module exception. The orchestrator
  (`main.py`) catches per item and continues.
- **Licence safety**: every clip copies `licence` and `source_url` from its source
  (`db.add_clip` refuses a source without a licence). Credit the source on screen
  and in the description.
- **Tests**: no network (a socket guard fails the test), no real credentials, no
  model downloads, no ffmpeg required (inject runners/backends). Reuse the
  fixtures in `tests/conftest.py`. One test module per source module (`tests/test_<module>.py`).

### Licence policy (fetch.check_licence must implement; strategy.py docstring repeats it)

- US federal government works are public domain (17 U.S.C. 105) **only when the
  government produced them**: NASA, official committee feeds (house.gov,
  senate.gov and the committees' own channels), DVIDS, NARA. **C-SPAN's own
  broadcasts are copyrighted by C-SPAN**; news-outlet re-uploads are not
  government works - reject (`blocklist_channels`). NASA/JPL uploads may embed
  third-party material: reject if the description asserts copyright / "all rights reserved" / licensed music.
- CC content must be **CC BY** (yt-dlp `license` contains "Creative Commons Attribution"
  and not NonCommercial/NoDerivs/ShareAlike). NC, ND, SA -> reject.
- Licence ids stored in `sources.licence` / `clips.licence`: `us_gov_public_domain`, `cc_by`.
- `sources.licence_evidence` = JSON with the raw metadata that justified the decision
  (`{"extractor", "channel", "channel_id", "uploader", "license", "webpage_url", "matched_rule", "description_excerpt"}`).

## Foundation API (implemented)

### config.py
- `get_settings() -> Settings` (cached), `reset_settings()`, `set_settings(s)`, `load_settings(environ=None)`.
- `Settings` (frozen dataclass; `.replace(**kw)`, `.ensure_dirs()`, `.twilio_configured`):
  `data_dir, db_path, downloads_dir, transcripts_dir, renders_dir, strategy_path,
  anthropic_api_key, anthropic_base_url, haiku_model="claude-haiku-5-5",
  sonnet_model="claude-sonnet-5-5", haiku_effort="low", sonnet_effort="medium",
  llm_max_retries=3, llm_timeout_sec=120, llm_server_fallbacks=True, llm_price_overrides,
  fake_llm=False, daily_llm_budget_usd=1.0, max_daily_llm_budget_usd=5.0,
  max_uploads_per_day=6, dry_run=True, youtube_client_secrets, youtube_token_path,
  youtube_default_privacy="private", youtube_category_id="28", twilio_account_sid,
  twilio_auth_token, twilio_from_number, owner_phone, owner_name="Blake",
  public_base_url, dashboard_token, quiet_hours_start="22:00", quiet_hours_end="08:00",
  urgent_call_dedup_hours=6.0, twilio_voice, twilio_language, youtube_daily_quota=10000,
  youtube_api_key, youtube_monetary_analytics=False, scout_cc_backend="ytdlp",
  scout_cc_api_max_searches_per_day=2, timezone="America/New_York", whisper_model="small.en",
  whisper_device="auto", whisper_compute_type="int8", ffmpeg_bin="ffmpeg", log_level,
  source_media_retention_days=3, render_retention_days=14`.
- Never read `os.environ` in modules: add a `Settings` field (+ `config.env.example` line,
  + conftest scrub if it has a new prefix) instead.
- Env vars are documented in `config.env.example`. `CLIPAGENT_DATA_DIR`,
  `CLIPAGENT_DB_PATH`, `CLIPAGENT_MEDIA_DIR`, `CLIPAGENT_STRATEGY_PATH`,
  `CLIPAGENT_CONFIG_FILE` relocate everything (tests use them).

### db.py
Connections: `connect(path=None)` (WAL, FKs, `sqlite3.Row`, auto-migrates on first use),
`init_db(path=None) -> schema_version`, `schema_version(path=None)` (read-only, 0 = never
initialised), `transaction(conn=None)` context manager. Schema v2 (in `MIGRATIONS`, upgrades a v1
DB in place): uploads gained `lifetime_views/likes/comments`, `lifetime_fetched_at`,
`youtube_status` and the `superseded` status; stats gained `estimated_revenue`, `cpm`
(NULL unless the monetary scope is granted); new `retention` table. Beware: `_split_sql`
splits on `;`, so never put a `;` inside a SQL comment.
Every helper takes keyword `conn=` to join a caller's transaction.
Exceptions: `NotFound`, `InvalidTransition`. Constants: `*_STATUSES`, `*_TRANSITIONS`,
`DECISION_KINDS`, `RESOLVED_VIA`, `NOTIFICATION_CHANNELS`, `UPLOAD_QUOTA_UNITS=1600`.
Row dataclasses (JSON columns decoded, 0/1 -> bool): `Source, Clip, Upload, Stat,
ChannelStat, Decision, LLMUsageRow, StrategyVersion, Notification, Event, Retention` (`.to_dict()`).

Time: `utcnow()`, `now_iso()`, `to_iso(dt)`, `parse_iso(s)`, `local_tz()`, `local_now()`,
`local_today()`, `local_day_bounds(day=None)`, `local_date_of(iso)`.

| Table | Helpers |
|---|---|
| sources | `add_source(url, source_type, *, platform, external_id, channel, channel_id, title, description, duration_sec, published_at, topic, licence, licence_evidence, metadata) -> id or None if URL known`, `get_source`, `require_source`, `get_source_by_url`, `list_sources(status=None\|str\|list, *, source_type, limit)` (oldest first), `update_source(id, **cols)` (non-status cols), `set_source_status(id, status, reason=None, *, force=False)`, `count_sources_by_status()` |
| clips | `add_clip(source_id, start_sec, end_sec, *, title, description, hashtags, hook_text, credit_text, haiku_score, sonnet_rationale, strategy_version, experiment_id, experiment_variant) -> id` (copies licence+url), `get_clip`, `require_clip`, `list_clips(status, *, source_id, limit)`, `update_clip(id, **cols)`, `set_clip_status(id, status, reason=None, *, force=False)`, `count_clips(*, experiment_id, variant, source_id, status) -> int` |
| uploads | `add_upload(clip_id, *, privacy, publish_at=None, dry_run=False) -> id` (queued), `mark_upload_succeeded(id, youtube_video_id, *, quota_units=1600)`, `mark_upload_failed(id, error, *, quota_units=0)`, `update_upload(id, **cols)`, `get_upload`, `get_upload_by_video`, `list_uploads(status, *, clip_id, include_dry_run, limit)` (newest first), `count_uploads_on(day=None)` (status uploaded by local `uploaded_at` day, dry runs included - **this is what the cap counts**), `quota_used_on(day)`, `publish_times_between(start_iso, end_iso)`, `live_video_ids()` (non-dry-run), `set_copyright_claim(video_id, claimed=True)`, `set_upload_lifetime(video_id, *, views, likes, comments)`, `set_youtube_status(video_id, status)` |
| stats / channel_stats / retention | `upsert_stat(video_id, day, *, views, watch_minutes, avg_view_duration_sec, avg_view_pct, likes, comments, shares, subscribers_gained, subscribers_lost, retention, estimated_revenue, cpm)` (`retention` column = legacy copy on the newest day only), `get_stats(video_id)`, `stats_between(start_day, end_day)`, `upsert_retention(video_id, curve, *, start_day, end_day)`, `get_retention(video_id) -> Retention`, `upsert_channel_stat(day, *, subscribers, total_views, video_count)`, `list_channel_stats(limit=90)`, `clip_performance(since_day=None, until_day=None) -> list[dict]` - one row per real uploaded/removed clip; **the period filters stats days, not upload date** (a clip uploaded earlier still reports views earned in the period; new clips with no data appear with zeros). Extra keys: `topic`, `avg_view_duration_sec`, `upload_status`, `youtube_status`, `estimated_revenue`, `total_views` (all days), `lifetime_views/likes/comments` |
| decisions | `add_decision(kind, summary, payload=None, *, expires_at=None) -> id`, `get_decision`, `list_decisions(status=None, *, limit)`, `resolve_decision(id, status, *, via, note=None) -> bool`, `set_decision_call_sid`, `get_decision_by_call_sid`, `expire_decisions() -> n` |
| llm_usage | `insert_llm_usage(...)` (use `budget.record_usage`), `llm_spend_between`, `llm_usage_summary(day=None)`, `list_llm_usage(limit)` |
| strategy_versions | `add_strategy_version(version, json, *, report, decision_id)` (use `strategy.save_strategy`), `get_strategy_version`, `latest_strategy_version`, `list_strategy_versions(limit)`, `strategy_version_for_decision(decision_id)` |
| notifications | `add_notification(kind, body, channel, *, status="queued", sid, error, decision_id) -> id`, `update_notification(id, *, status, sid, error)`, `list_notifications(status=None, *, limit)` (queued = oldest first), `get_notification(id)`, `get_notification_by_sid(sid)` |
| events | `log_event(module, message, *, level="info", data=None)`, `list_events(*, limit, level, module, since)` (newest first) |
| kv | `kv_get(key, default)`, `kv_set`, `kv_set_if_absent -> bool`, `kv_incr(key, by=1) -> int` (atomic counter), `kv_delete` (pause switch, once-per-day flags, scout cursors, quota tally, retry counts) |

### strategy.py
- `load_strategy(path=None) -> Strategy` (a validated `dict` subclass; `.version`, `.topics`,
  `.source_types`, `.clip_length`, `.experiment`, `.allowlist_for(type)`, `.updated(**kw)`, `.to_json()`).
- `validate_strategy(data) -> list[str]`, `parse_strategy(data) -> Strategy` (raises `StrategyError(errors)`).
- `save_strategy(new, *, report=None, decision_id=None, path=None) -> Strategy`: validates,
  version = max(file, DB)+1, stamps `updated_at`, atomic write, inserts `strategy_versions`.
- `record_initial_version()` - seed `strategy_versions` with the shipped file (main `init`).
- `diff_major_changes(old, new) -> list[MajorChange(kind, summary, detail)]`; kinds:
  `new_niche` (niche text changed or topic area added), `new_source_type`, `budget_increase`,
  `strategy_change` (any allowlist edit, blocklist addition/removal, source type removed).
  The strategist adds only hard refusals on top (`strategist.licence_problems`).
- `validate_strategy` rejects `any_channel: true` outside `ANY_CHANNEL_SOURCE_TYPES` ({cc_by_youtube}).
- Optional key `max_windows_per_source` (default 40, `Strategy.max_windows_per_source`): Haiku
  windows per source; pick falls back to kv `pick:max_windows` only when the key is absent.
- `upload_cap(strategy=None) -> int` = min(strategy.uploads_per_day, MAX_UPLOADS_PER_DAY).
- `active_experiment(strategy, day=None)`, `assign_variant(strategy, key, day=None) -> (experiment_id, "control"|"variant") | (None, None)` (even key = control).
- `is_publisher_allowed(strategy, source_type, *, channel, channel_id, handle, url) -> (ok, reason)` -
  shared allowlist/blocklist rule for scout (pre-filter) and fetch (authoritative).
- Schema documented in the module docstring. `caption_style` keys: `font, font_size,
  primary_color, highlight_color, outline_color, outline_px, max_words_per_line,
  position (top|center|lower_third), uppercase, animation (word_pop|karaoke|none),
  framing (auto|center_crop|blur_fill|fit, optional)` - enum values are validated.
- Caption font: `edit.resolve_font` tries the strategy font, then `edit.FONT_FALLBACKS`
  (Montserrat ExtraBold/Black, Montserrat, Anton, Inter Black, ..., DejaVu Sans); TTF/OTF files
  in `assets/fonts/` are used by ffmpeg and count as installed.

### budget.py
- `PRICES: dict[model, ModelPrice]`, `price_for(model)`, `cost_usd(model, in, out, cache_read=0, cache_write=0)`,
  `estimate_cost(model, input_tokens, max_output_tokens)`.
- `record_usage(model, purpose, *, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0, tier, source_id, clip_id, request_id, fake) -> cost_usd`.
- `daily_cap_usd()` = min(strategy.daily_llm_budget_usd (or DAILY_LLM_BUDGET_USD if strategy unreadable), MAX_DAILY_LLM_BUDGET_USD).
- `spent_today(day=None)`, `remaining_today()`, `check_budget(estimated_usd=0) -> remaining` (raises `BudgetExceeded(spent, cap, estimated)`),
  `is_paused()` (manual pause or cap reached), `set_paused(bool, reason)`.
- First cap hit per local day -> `warning` event + queued `notifications` row (kind `budget_cap`, channel `call`). `notify.send_queued()` delivers it.
- Fake-mode usage is recorded with `fake=1` and **counts** toward the cap.

### llm.py
```python
complete(tier: Literal["haiku","sonnet"], *, system: str | Sequence[str], user: str, purpose: str,
         max_tokens: int = 2048, cache_system: bool = True, json_schema: dict | None = None,
         effort: str | None = None, source_id: int | None = None, clip_id: int | None = None,
         json_retries: int = 1) -> LLMResult
LLMResult(text, data, usage: Usage, cost_usd, model, tier, stop_reason, fake, request_id, attempts)
Usage(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
```
- Put stable text (instructions, `strategy` JSON) in `system` (cached; breakpoint on the
  last block; no timestamps/ids there). Per-call content goes in `user`.
- `purpose` is `"<module>.<step>"`, e.g. `pick.score_window`, `pick.finalize`, `strategist.review`.
- `json_schema`: plain JSON schema; `minimum/maximum/maxLength/maxItems...` are stripped for
  the API and validated client-side; every object becomes `additionalProperties: false`.
  `res.data` is the parsed, validated object. Errors: `LLMOutputError` (`.text`, `.truncated`),
  `LLMRefusal`, `LLMError`, `budget.BudgetExceeded`; every `LLMError` carries `.cost_usd`
  (already recorded) and `.attempts`.
- **A client-side constraint violation (`maxLength`, `maxItems`, `minimum`...) triggers a paid
  retry of the whole call.** Leave length/count limits out of schemas and trim in code (pick and
  strategist do).
- Truncation (`stop_reason == "max_tokens"`): the retry doubles `max_tokens` (cap 16384) only if
  that worst case fits the remaining budget; otherwise `LLMOutputError(truncated=True)` at once.
- Thinking is adaptive (model default); `effort` defaults: haiku `low`, sonnet `medium`.
  Thinking tokens bill as output - leave headroom (Haiku scoring: ~1024; Sonnet: ~4096).
- Sonnet calls use the beta endpoint with `fallbacks="default"` (server-side refusal fallback).
- Fakes: `set_fake_handler(fn)`, `fake_handler(fn)` context manager, `reset()`, `set_client(client)`.
  A handler receives an `LLMRequest(tier, model, system, user, purpose, max_tokens, json_schema,
  effort, cache_system, source_id, clip_id, attempt)` and returns `str` | `dict`/`list` | `FakeReply(text, data, usage, stop_reason, model)`.
  Default fake: deterministic sample of `json_schema` (integers = midpoint of min/max; strings `"fake"`; enums = first value).
- Helpers: `to_api_schema`, `validate_json(value, schema) -> list[str]`, `sample_from_schema`, `parse_json_text`, `approx_tokens`.

### Test fixtures (`tests/conftest.py`)
Autouse: isolated temp data dir/DB/strategy copy, `FAKE_LLM=true`, `DRY_RUN=true`,
credentials scrubbed, socket guard. Opt-in: `settings`, `strategy`, `fake_llm`
(`.queue(*replies)`, `.by_purpose[p] = reply|fn`, `.handler`, `.calls`, `.purposes()`),
`clock` (frozen at 2026-11-13T15:00Z = 10:00 New York; `.set(dt)`, `.advance(**timedelta)`),
`make_source(status="candidate", **cols) -> id`, `make_clip(source_id=None, status="picked", **cols) -> id`,
`make_upload(clip_id=None, *, video_id, dry_run, publish_at, succeeded=True) -> id`.

## Module interfaces (implemented; extend, don't rename)

Files and paths: downloads `settings.downloads_dir / f"{source_id}" /` (`video.mp4`, `info.json`),
transcripts `settings.transcripts_dir / f"{source_id}.json"`, renders `settings.renders_dir / f"{clip_id}.mp4"`.
Paths stored in the DB are absolute strings.

### scout.py
```python
@dataclass class Candidate: url, source_type, platform, external_id, channel, channel_id, title,
                            duration_sec, published_at, topic, metadata: dict
Searcher = Callable[[str, int, Strategy], list[Candidate]]   # (query, limit, strategy)
SEARCHERS: dict[str, Searcher]          # per source_type; tests replace entries
def scout(max_candidates: int | None = None, strategy: Strategy | None = None) -> list[int]
```
For each strategy topic x its source types: search (yt-dlp `ytsearchN:` / YouTube CC filter /
NASA images API via httpx), pre-filter with `strategy.is_publisher_allowed` and duration
(>= clip_length.max, <= 3 h), insert with `db.add_source(..., topic=query)`; returns only new
ids (default cap 10 per run). No LLM. Logs one summary event.

### fetch.py
```python
@dataclass(frozen=True) class LicenceDecision: ok: bool; licence: str | None; reason: str; evidence: dict
class LicenceRejected(Exception); class FetchError(Exception)
def extract_info(url: str, *, download: bool, outdir: Path | None = None) -> dict   # yt-dlp wrapper; tests monkeypatch
def check_licence(info: dict, strategy: Strategy, source_type: str) -> LicenceDecision   # pure
def fetch_source(source_id: int) -> Path
```
`check_licence` uses the yt-dlp info dict (`webpage_url, extractor_key, channel, channel_id,
uploader, uploader_id, license, description`) + `is_publisher_allowed` + the licence policy above.
`fetch_source`: candidate only; probes metadata first, on not-ok sets `rejected` (reason) and raises
`LicenceRejected` before downloading; else downloads (mp4, <=1080p, merges audio), updates
`licence, licence_evidence, video_path, duration_sec, title, channel...`, status `fetched`, returns video path.

### transcribe.py
```python
class Backend(Protocol):
    name: str
    def transcribe(self, media_path: Path) -> dict   # returns {"language", "segments"} in the shape below
def set_backend(backend: Backend | None) -> None     # None = faster-whisper from settings
def transcribe_source(source_id: int) -> Path
def load_transcript(path: Path | str) -> dict
def words_between(transcript: dict, start: float, end: float) -> list[dict]   # words fully inside [start, end]
class TranscribeError(Exception)
```
Transcript JSON (UTF-8, times in seconds from media start, floats rounded to 3 dp):
```json
{"source_id": 12, "media_path": "/abs/video.mp4", "backend": "faster-whisper:small.en",
 "language": "en", "duration_sec": 612.4, "created_at": "2026-11-13T15:00:00Z",
 "segments": [{"id": 0, "start": 0.0, "end": 4.2, "text": "Hello and welcome.",
               "words": [{"start": 0.0, "end": 0.42, "word": "Hello", "prob": 0.98}, ...]}]}
```
`word` has surrounding whitespace stripped; punctuation stays attached. Sets `transcript_path`, status `transcribed`.

### pick.py
```python
@dataclass(frozen=True) class Window: start_sec: float; end_sec: float; text: str
def make_windows(transcript: dict, length_sec: float = 60, stride_sec: float = 30) -> list[Window]
def credit_line(source: db.Source) -> str        # e.g. "Video: NASA (public domain)" / "Video: <channel> (CC BY)"
def pick_clips(source_id: int, strategy: Strategy | None = None) -> list[int]
```
Haiku `pick.score_window` per window -> `{"score": int 1-10, "reason": str, "hook": str}`; keep
`score >= min_haiku_score`; top `windows_to_escalate` -> one Sonnet `pick.finalize` call per source
returning `{"clips": [{"start_sec", "end_sec", "title", "description", "hashtags": [...],
"hook_text", "rationale"}]}` (<= `max_clips_per_source`); snap to word boundaries, clamp to
`clip_length_sec`, `db.add_clip(..., strategy_version=strategy.version, experiment via assign_variant)`.
Source -> `picked` (or `done` if nothing qualified). `BudgetExceeded` propagates; source stays `transcribed`.

### edit.py
```python
def build_ass(words: list[dict], clip_start: float, caption_style: dict, *, hook_text: str = "",
              credit_text: str = "", duration: float) -> str              # pure; ASS subtitle file text
def build_ffmpeg_cmd(src: Path, ass_path: Path, out: Path, start: float, end: float,
                     settings: Settings) -> list[str]                    # pure
def run_ffmpeg(cmd: list[str]) -> None                                   # subprocess; tests monkeypatch
def render_clip(clip_id: int, strategy: Strategy | None = None) -> Path
class RenderError(Exception)
```
Output: 1080x1920 H.264 + AAC, center crop (or blurred-fill when source is not 16:9),
animated word captions per `caption_style`, hook overlay first ~3 s, credit line always visible.
Sets `render_path`, status `rendered`.

### upload.py
```python
class UploadCapReached(Exception); class UploadError(Exception)
def uploads_today() -> int                                   # db.count_uploads_on()
def next_publish_slots(n: int, after: datetime | None = None) -> list[datetime]   # aware UTC
def upload_clip(clip_id: int, publish_at: datetime | None = None) -> int          # upload_id
def get_youtube_service()                                    # OAuth installed-app flow; never in tests
```
`next_publish_slots`: strategy `posting_times` in strategy `timezone`, strictly after `after`
(default now), skipping slots already in `db.publish_times_between`, at most `upload_cap()` per local day.
`upload_clip`: requires clip `rendered|scheduled`; raises `UploadCapReached` if `uploads_today() >= strategy.upload_cap()`;
`publish_at=None` -> first free slot; creates `db.add_upload` row, then videos.insert (privacy
`private` + `publishAt` when scheduled, `#Shorts` in title/description, credit + source URL + licence
in description, categoryId from settings); `mark_upload_succeeded` / `mark_upload_failed`; clip -> `uploaded`.
DRY_RUN: no API call, video id `dryrun-<clip_id>`, quota 0.

### analytics.py
```python
def set_service_factory(fn: Callable[[], tuple[Any, Any]] | None) -> None   # (youtube_data, youtube_analytics); tests inject fakes
def collect_stats(day: date | None = None) -> int     # rows upserted into stats
def refresh_channel_stats() -> None                    # today's channel_stats row
def check_claims() -> int                              # flags copyright claims / blocked videos; returns count newly flagged
```
`day` defaults to local today minus 2 (Analytics lag). Polls `db.live_video_ids()`; DRY_RUN or no
token -> logs and returns 0.

### strategist.py
```python
@dataclass class StrategyReview: report: str; proposed: dict; major_changes: list[MajorChange];
                                 decision_id: int | None; applied_version: int | None
def weekly_review(days: int = 7) -> StrategyReview
def apply_decision(decision_id: int) -> bool          # apply an approved decision's payload
```
Gathers `db.clip_performance(since)`, `db.list_channel_stats`, spend, current strategy; one Sonnet
call `strategist.review` -> `{"report": str, "strategy": {...full strategy...}, "experiment": {...}}`
(exactly one experiment). Invalid proposal -> keep current, log error. No major changes ->
`save_strategy(proposed, report=...)` now. Major changes -> `notify.request_approval(kind, summary,
{"action": "apply_strategy", "strategy": proposed, "report": report})` and nothing is saved
until approved (kind = the single change's kind, or `strategy_change` if several). Report is sent via
`notify.notify("weekly_report", ...)`.

### notify.py
```python
def call(text: str, *, kind: str = "call", decision_id: int | None = None) -> int      # notification id
def sms(text: str, *, kind: str = "sms", decision_id: int | None = None) -> int
def notify(kind: str, text: str, *, urgent: bool = False) -> int   # urgent: call, SMS fallback; else SMS
def request_approval(kind: str, summary: str, payload: dict, *, expires_hours: int = 48) -> int  # decision id
def handle_decision(decision_id: int, approved: bool, *, via: str) -> bool   # resolve + strategist.apply_decision (late import)
def send_queued() -> int                                # deliver queued notifications rows (e.g. budget_cap)
def daily_summary() -> str                              # compose + send; returns text
router: fastapi.APIRouter   # prefix "/twilio"
```
Every send writes a `notifications` row; DRY_RUN or Twilio unconfigured -> status `dry_run`, nothing sent.
Approval call: TwiML `<Say>` summary + `<Gather numDigits=1>` -> 1 = approve, 2 = reject; no answer
-> SMS "Reply YES <id> or NO <id>". Routes: `POST /twilio/voice/{decision_id}`,
`POST /twilio/gather/{decision_id}`, `POST /twilio/sms` (YES/NO <id>, PAUSE, RESUME, STATUS),
`POST /twilio/status` (call status callback -> SMS fallback). Validate `X-Twilio-Signature`
when a Twilio auth token is configured. Webhook base: `settings.public_base_url`.

### dashboard.py / server.py
```python
# dashboard.py
router: fastapi.APIRouter   # prefix "/dashboard", Jinja2 templates in templates/
# pages: "/" overview (spend today vs cap, uploads today vs cap, pending decisions, recent events),
# "/clips" (db.clip_performance), "/sources", "/strategy" (versions + current), "/decisions"
# (POST "/decisions/{id}/approve|reject" -> notify.handle_decision(via="dashboard")),
# JSON: "/api/summary". Auth: if DASHBOARD_TOKEN set, require ?token= or Bearer header.

# server.py
def create_app() -> fastapi.FastAPI    # includes notify.router + dashboard.router, GET /healthz, db.init_db() in lifespan
app = create_app()
```
The dashboard is imported defensively (warning logged, app still serves `/twilio/*` if it fails);
`GET /` redirects to `/dashboard/` (or `/healthz` without a dashboard); `/healthz` returns
`{"status", "schema_version", "dashboard"}`.

### main.py
```python
def process_source(source_id: int, *, allow_llm: bool = True, strategy=None) -> list[int]  # rendered clip ids
def render_clips(source_id: int | None = None, strategy=None) -> list[int]
def retry_failed_sources() -> list[int]          # failed -> resume stage, max MAX_SOURCE_RETRIES=2, >= 50 min apart
def process_pending(max_sources: int = 3) -> dict  # the hourly "process" job
def upload_ready(limit: int | None = None) -> list[int]   # upload ids
def run_once(max_sources: int = 3) -> dict        # scout (if queue short) + process_pending + upload_ready
def manual(url: str, source_type: str | None = None, *, upload: bool = True) -> list[int]  # clip ids
def infer_source_type(url, strategy) -> str      # first enabled type whose licence check passes
def requeue_dry_run() -> dict                    # DRY_RUN -> live: dry-run uploads superseded, clips back to rendered/picked
def cleanup() -> dict                            # disk hygiene (never deletes DB rows, info.json, transcripts)
def run_job(name, fn)                            # job wrapper: kv main:job:<name>, error event, one job_error SMS/day
def build_scheduler() -> BackgroundScheduler     # coalesce, max_instances=1, misfire grace 30 min
def doctor_checks(*, network=False) -> list[Check];  def status_text() -> str
def main(argv: list[str] | None = None) -> int
```
Error policy: `LicenceRejected` permanent; `FetchError`/`TranscribeError`/`PickError` per source
(bounded retry); `RenderError` per clip (`MAX_CLIP_RETRIES=2`); `BudgetExceeded` stops LLM work
for the run (source stays `transcribed`) but rendering/uploading go on; `upload_ready` stops on
`UploadCapReached`/`QuotaExhausted`/`UploadAuthError`, skips other `UploadError`s.
Locks (`fcntl` in the data dir): `scheduler.lock` (one scheduler per data dir), `process.lock`,
`upload.lock` (a concurrent CLI run gets `Busy`; a job skips).
A source becomes `done` once none of its clips still needs rendering.

CLI: `python main.py init | manual <url> [--source-type T] [--no-upload] | scout | process [--limit N]
| run-once [--max-sources N] | upload [--limit N] | analytics [--day YYYY-MM-DD] | review | summary
| serve [--host --port] | schedule [--host --port] | status | doctor [--network] | requeue-dry-run [--force]
| cleanup | approve <id> | reject <id>`.
Jobs (local time, off the hour): scout 06:10; process hourly at :20; upload hourly at :40;
send_queued every 5 min; expire_decisions hourly at :50; analytics 07:25 (collect_stats +
refresh_channel_stats + check_claims, each isolated); cleanup 03:35; daily_summary 20:55;
weekly_review Sunday 18:05. `schedule` runs the scheduler and uvicorn together and exits 0 on SIGTERM.

### Notification kinds
| kind | producer | channel | rings in quiet hours |
|---|---|---|---|
| `budget_cap` | budget | call (queued) | yes |
| `youtube_auth` | youtube_auth | call (queued) | yes |
| `youtube_claim` | analytics | call (queued) | yes |
| `youtube_quota` | upload | sms (queued) | no |
| `youtube_private_lock` | analytics | sms (queued) | no |
| `job_error` | main.run_job (once per job per day) | sms (queued) | no |
| `weekly_report`, `strategy_conflict` | strategist via `notify.notify` | sms | no |
| `approval` | notify.request_approval | call, SMS fallback | no (queued) |
| `daily_summary` | notify.daily_summary | call | no |

`notify.URGENT_KINDS` = the "yes" rows; add a kind there if it must wake the owner. Queued rows of
any kind are delivered by `notify.send_queued()` (every 5 min).

### Extension points
`scout.SEARCHERS[type]`, `transcribe.set_backend`, `edit.set_crop_locator` (face tracking),
`llm.set_fake_handler` / `set_client`, `upload.set_service_factory`, `analytics.set_service_factory`,
`notify.set_client`, `fetch.extract_info` (monkeypatch), `main.job_specs()` (jobs).

## Phase 2 — assistant layer

The owner's personal assistant; the Shorts pipeline is one division. Architecture, memory,
safety model and how-tos: `docs/ASSISTANT.md`. Everything lives in new files - the package
`assistant/` (flat modules inside it), `agents/*.md`, `skills/<name>/SKILL.md`, `vault_template/`,
`tests/test_assistant_*.py` (+ shared fixtures in `tests/assistant_fixtures.py`, used via
`pytestmark = pytest.mark.usefixtures("assistant_env")`); env vars in `config.env.example` (Phase 2 section).
Phase-1 conventions above apply (UTC ISO in DB, `db.utcnow()`, no import side effects, DRY_RUN,
`db.log_event`, offline tests).

- **Anthropic SDK**: besides `llm.py`, `assistant/runtime.py` (`AnthropicBackend`) is the one other
  place allowed to import `anthropic`: the assistant needs a multi-turn tool-use loop with sub-agents,
  which `llm.complete` (single-shot JSON) does not do. Nothing else in `assistant/` touches the SDK.
- **Privacy**: the vault (default `<data_dir>/vault`) holds personal data and is never committed;
  `tests/test_assistant_settings.py` fails if the default path is not git-ignored.
- **Spend**: assistant calls write `llm_usage` rows with `purpose LIKE 'assistant.%'`, `tier='assistant'`,
  and are capped by `ASSISTANT_DAILY_BUDGET_USD` / `ASSISTANT_MAX_TURN_USD`, not the pipeline cap.

### assistant/settings.py
`AssistantSettings` (frozen; `.replace(**kw)`): `enabled=False, name="Sterling", owner_honorific="sir",
owner_name, model="claude-fable-5-2", subagent_model="claude-opus-5-5", effort="medium",
subagent_effort="low", daily_budget_usd=3.0, max_turn_usd=0.5, max_sms_per_day=8, max_tool_rounds=8,
max_tokens=2048, subagent_max_tokens=3072, web_search=False, vault_dir, fake (= FAKE_ASSISTANT,
default FAKE_LLM), timezone, dry_run`. `get_assistant_settings()` (cached; re-read whenever
`config.reset_settings()` runs), `load_assistant_settings(environ=None)`, `set_assistant_settings(s|None)`,
`reset_assistant_settings()`.

### assistant/store.py (tables `as_*` in the pipeline DB; own `MIGRATIONS`, kv `assistant:schema_version`)
`init_store(conn=None) -> version`, `schema_version()`, `transaction(conn=None)` (migrates lazily), `week_start_of(day=None) -> date` (local Monday).
Every helper takes `conn=`. Constants: `DIRECTIONS, CHANNELS (sms|dashboard|call|system), CHECKIN_STATUSES
(pending|answered|skipped|expired), PAY_SCHEDULES, INCOME_SOURCES (job|reels|other), MEAL_SLOTS, MEAL_STATUSES
(suggested|accepted|declined|eaten), GOAL_STATUSES (active|done|dropped), RUN_STATUSES (running|ok|error|budget|denied),
LESSON_STATUSES (proposed|adopted|retired)`. Rows (`.to_dict()`): `Message, Checkin, Job, WorkLog, Income, Meal, Goal, AgentRun, Lesson`.
- messages: `add_message(direction, channel, body, *, kind="chat", agent, cost_usd, reply_to, meta) -> id`, `get_message`,
  `list_messages(limit=50, *, channel, direction, since)` (newest first), `count_outbound_between(start_iso, end_iso, *, channel="sms", kinds_excluded=("reply",))`
- check-ins: `add_checkin(kind, question, scheduled_for_iso) -> id`, `get_checkin`, `list_checkins(status, *, kind, limit)`,
  `due_checkins(now_iso=None)` (pending, unsent, due), `open_checkin(kind=None)` (latest sent, unanswered),
  `mark_checkin_sent(id, *, message_id)`, `answer_checkin(id, answer_text, parsed=None)`, `set_checkin_status(id, status)`
- jobs/hours/income: `upsert_job(name, *, hourly_rate, pay_schedule="biweekly", est_tax_rate=0.0, active=True) -> id`,
  `get_job`, `get_job_by_name` (case-insensitive), `list_jobs(*, active_only=True)`,
  `log_hours(job_id, hours, *, week_start=None, source="sms", note) -> id` (upsert per job+Monday, 0-168),
  `get_work_log(job_id, week_start=None)`, `list_work_log(*, job_id, since_week, limit)` (newest week first),
  `add_income(amount_usd, *, source, day=None, note, ref) -> id | None` (None = duplicate (source, ref)),
  `list_income(start_day, end_day, *, source)` (inclusive), `income_total(...) -> float`
- meals: `add_meal(slot, suggestion, *, day=None) -> id`, `update_meal(id, *, status, feedback)`, `get_meal`, `list_meals(*, since_day, slot, status, limit)`
- goals: `add_goal(title, *, metric, target, current, due) -> id`, `update_goal(id, **cols)`, `get_goal`, `list_goals(status="active"|None)`
- audit/spend: `record_agent_run(agent, task, *, model, duration_ms, cost_usd, status, summary, parent_id, tool_calls, fake) -> id`,
  `finish_agent_run(id, *, status, duration_ms, cost_usd, summary, tool_calls)`, `get_agent_run`, `list_agent_runs(limit, *, agent, since)`,
  `assistant_spend_between(start_iso, end_iso)`, `assistant_spent_today(day=None)` (llm_usage rows `assistant.*` only)
- lessons: `add_lesson(area, lesson, *, evidence, status="proposed") -> id`, `set_lesson_status(id, status)`, `list_lessons(status, *, area, limit)`

### assistant/vault.py (memory; all paths vault-relative `.md`; LLM-facing => strict path safety)
`vault_root()`, `ensure_vault() -> Path` (copies `vault_template/`, never overwrites), `safe_path(rel) -> (abs, rel_posix)`,
`read_note(rel) -> Note(path, meta, body, sha, mtime)`, `note_exists(rel)`, `write_note(rel, body, *, meta, base_sha, on_conflict="append"|"raise", source, note_type) -> Note`
(base_sha = sha you read; changed on disk or never read -> appended under "Merged ..." or `NoteConflict`),
`append_to_note(rel, text, *, heading=None, source, note_type, tags) -> Note`, `delete_note(rel)`, `list_notes(folder=None)`,
`links_in(text)`, `backlinks(rel_or_name)`, `journal_path(day)`, `append_journal(text, day=None, *, source) -> Note`,
`remember(fact, *, area, source, confidence="stated") -> RememberResult(path, added, duplicate_of)`, `forget(fact, *, area=None) -> list[str]`,
`note_for_area(area) -> (path, type)` (`AREAS` + `person:<Name>`, `project:<Name>`), `facts_in(body)`,
`search(query, limit=8) -> list[SearchHit(path, title, snippet, score)]`, `sync_index() -> IndexStats`, `reindex()`,
`context_for(text, budget_chars=4000, *, journal_days=3) -> str`, `parse_note_text(text)`, `render_note(meta, body)`.
Errors: `VaultError` > `VaultPathError`, `NoteNotFound`, `NoteConflict`.

### assistant/persona.py
`system_blocks(channel, *, agent="main"|AgentSpec, profile_context="", settings, agents, skills) -> list[str]` (stable, cacheable),
`lint(text, channel, *, settings) -> list[str]` (codes: empty, too_long, emoji, exclamation, markdown, url_in_voice,
honorific_overuse, as_an_ai, gush), `polish(text, channel, *, settings) -> str` (idempotent), `escape_untrusted(text)`,
`load_examples() -> list[Example(title, channel, situation, reply, bad, violates)]`, `CHANNEL_LIMITS` (sms 320, voice 600, dashboard 4000).

### assistant/registry.py
`load_skills(dir=None, *, strict=True) -> dict[str, SkillSpec(name, description, body, path, resources)]`,
`load_agents(dir=None, *, skills, known_tools, strict=True) -> dict[str, AgentSpec(name, description, model, tools, skills, prompt, path, extra)]`,
`AgentSpec.resolved_model(settings)` (`main` -> ASSISTANT_MODEL, `subagent` -> SUBAGENT_MODEL, else the id),
`validate_all(known_tools) -> list[str]`, `parse_frontmatter(text)`, `RegistryError(.errors)`.

### assistant/tools.py (the only capabilities)
`Tool(name, description, input_schema, fn(ctx, args), owner, side_effects)`, `ToolContext(channel, agent, source, message_id, calls)`,
`ToolCall`, `ToolResult(ok, content)`, `ToolError` (message shown to the model), `REGISTRY`, `register(tool, *, replace=False)`, `unregister(name)`,
`available(names) -> list[Tool]`, `known_tool_names()`, `dispatch(name, args, ctx, *, allowed) -> ToolResult`,
`load_extensions()` (imports `assistant.ledger|meals|checkins|improve` if present and calls their `register_tools(register)`),
`send_to_owner(text, *, kind="update", agent, reply_to, cost_usd) -> dict` (the single outbound path; `kind="reply"` = sent now, exempt from the cap),
`outbound_sms_today()`, `studio_snapshot(period_days=7) -> dict`, `register_approval_handler(name, fn(decision, data) -> bool)`,
`apply_approved_decision(decision_id) -> bool`.
Built-in tools: `memory_search, memory_read, memory_list, memory_remember, memory_forget, journal_append, studio_status, goals_list,
goals_update, checkin_schedule, skill_read, lessons_propose, lessons_list, request_approval, message_owner`.
**Reserved for the next engineers** (`PLANNED_TOOLS`; agents already list them; not offered until registered):
`ledger_log_hours, ledger_summary` (assistant/ledger.py), `meals_suggest, meals_record_feedback` (assistant/meals.py),
`improvement_propose, eval_results` (assistant/improve.py). `checkin_schedule` may be replaced by assistant/checkins.py
(`register(..., replace=True)`). Runtime-provided: `delegate` (main only), `web_search` (server tool, `ASSISTANT_WEB_SEARCH`).

### assistant/runtime.py
`run_turn(message, *, channel, context=None, deliver=False) -> TurnResult(text, cost_usd, agent_runs, tool_calls, status, message_in_id, message_out_id, raw_text)`
(status ok|budget|error|denied|rounds), `run_task(agent_name, task, *, channel="system", deliver=False, kind="task", context=None) -> TurnResult`,
`handle_inbound_sms(body) -> TurnResult | None` (None when ASSISTANT_ENABLED is false; run it in the background),
`set_backend(b|None)`, `get_backend()`, `reset()`, `FakeBackend(*steps)` (`.queue(...)`, `.queue_for(agent, ...)`, `.requests`;
steps: str | `fake_tool(name, **input)` | list of them | `ModelReply` | callable(req)), `AnthropicBackend(client=None)`,
`ModelRequest`, `ModelReply`, `Usage`, `ToolUse`, `AssistantBudgetExceeded(reason turn|daily|paused)`, `estimate_cost(req)`,
`check_budget(est, turn, settings)`, `remaining_today()`, `price_for(model)`, `cost_of(model, usage)`, `wrap_untrusted(source, text)`.

### Full-power Sterling (claude_code backend)
Main agent = real Claude Code with built-in tools (`claude_code.build_full_argv`, `permission_settings`: dontAsk +
allow/deny rules; secrets and destructive commands denied), our MCP tools, `--agents` from agents/*.md, skills mirrored to
`<data>/claude-code/skills`, one resumed session per channel (`session_for`, daily rollover -> journal). Self-edits:
`assistant/selfedit.py` (branch `sterling/self`, test -> commit + restart + /healthz, else reset; `undo()`; tests use
`selfedit.set_app_dir/set_runners`, conftest points it at a non-repo dir). Kill switch `runtime.set_stopped`
(SMS STOP/START STERLING, UNDO). Plan usage + deferred queue: `assistant/usage.py` (`as_deferred`, store v2;
tools `usage_status`, `schedule_prompt`; `usage.should_defer(kind, prompt)` for non-urgent jobs). `open_url` opens his
browser. Chat images: `home.save_upload` -> paths passed to the turn (`runtime.run_turn(attachments=)`). Details: docs/ASSISTANT.md.

### Jarvis HUD, hearing, self-check
`assistant/hud.py`: panel kinds (markup/image/video/youtube/map/data/list), tools `show_map` (Nominatim, kv cache,
1 req/s), `show_media`, `show_data`, `ui_theme|ui_ring|ui_rails|ui_effect|ui_reset` (kv `hud:ui`, `ui` events),
`fetch_remote` = the SSRF-guarded proxy behind `/voice/api/img` and `/voice/api/media` (tests: `hud.set_http`,
`hud.set_resolver`; conftest resets). `claude_code.user_mcp_servers()` passes his `~/.claude.json` MCP servers into
the brain (`ASSISTANT_MCP_SERVERS`; names only anywhere visible; tests: `claude_code.set_claude_json`).
`VOICE_STT=auto|browser|server` (page speech recognition vs Whisper). `assistant/selfcheck.py` (`main.py assistant
selfcheck`, Settings button, tool `selfcheck_run`): real tool calls, every change undone via `admin.app_undo`.
