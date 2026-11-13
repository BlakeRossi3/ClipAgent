# Twilio setup (phone calls and texts)

The agent calls you for the daily summary, urgent problems and approval requests, and texts
you when a call isn't answered. You can text it back. This guide covers the one-time setup.

Until it's done, leave `DRY_RUN=true`. In dry-run mode nothing is sent. Each would-be call or
text is saved as a `notifications` row with status `dry_run` and shows up on the dashboard.

## 1. Account and phone number

1. Create a Twilio account at twilio.com and upgrade it from trial (see section 5 for why).
2. Console → **Phone Numbers → Manage → Buy a number**. Pick a US local number with both
   **Voice** and **SMS** capability.
3. Copy the **Account SID** and **Auth Token** from the Console home page.

## 2. Environment variables (`config.env`)

| Variable | Example | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | `ACxxxxxxxx…` | |
| `TWILIO_AUTH_TOKEN` | `…` | Also used to verify webhook signatures. Webhooks are refused (403) without it unless `DRY_RUN=true`. |
| `TWILIO_FROM_NUMBER` | `+15550001111` | The number you bought, in E.164 format |
| `OWNER_PHONE` | `+15551234567` | Your mobile. Only texts from this number are obeyed, and keypad approvals count only on calls to it. |
| `OWNER_NAME` | `Blake` | Used in greetings |
| `PUBLIC_BASE_URL` | `https://agent.example.com` | The public HTTPS address of the server, with no trailing slash. See section 4. |
| `TIMEZONE` | `America/New_York` | Sets quiet hours and what counts as "today" |
| `DRY_RUN` | `false` | Turn this off only after everything below works |

Defaults (settings in `config.env`):

- Quiet hours are 22:00–08:00 local time (`QUIET_HOURS_START` / `QUIET_HOURS_END`). Non-urgent
  texts and calls wait until the window ends. Urgent problem calls (AI budget cap, YouTube login
  problems, copyright claims / takedowns) can still ring at night.
- The same urgent problem calls at most once every 6 hours (`URGENT_CALL_DEDUP_HOURS`). Repeats
  come as texts instead.
- At most 25 calls + texts reach your phone per day (`notify.MAX_NOTIFICATIONS_PER_DAY`), as a
  backstop against a runaway loop; the rest are only recorded on the dashboard.
- The voice is `Polly.Joanna-Neural` (`TWILIO_VOICE`).

## 3. Webhook URLs

| What | Where to set it | URL |
|---|---|---|
| Incoming SMS | Console → Phone Numbers → your number → **Messaging configuration** → "A message comes in" → Webhook, **HTTP POST** | `{PUBLIC_BASE_URL}/twilio/sms` |
| Call content / keypad | Set automatically on each call. Nothing to configure. | `/twilio/voice/{decision_id}`, `/twilio/gather/{decision_id}`, `/twilio/say/{notification_id}` |
| Call status callback | Set automatically on each call. Nothing to configure. | `/twilio/status` |

You can leave the number's "A call comes in" voice setting alone. The agent only places calls
and never answers them.

Texts you can send to the agent:

- `YES 12` or `NO 12` answers approval #12. The number is required: a bare `YES` gets a reply
  listing what is pending, so an answer can never land on a different request than you meant.
- `STATUS` returns uploads today, AI spend against the cap, and pending approvals.
- `PAUSE` and `RESUME` stop and restart AI spending.
- `HELP` lists the commands.

On an approval call, press 1 to approve, 2 to reject, or 9 to hear the request again. If you
don't answer, voicemail picks up, or no valid key is pressed after 3 prompts, you get a text
saying "Reply YES <id> or NO <id>".

## 4. Expose the server over HTTPS

Twilio must reach `server.py` (`python main.py schedule` or `serve`, port 8000 by default) at a public
HTTPS URL. Pick one of these:

- **Reverse proxy on a server** with a domain. Caddy is the simplest because it gets TLS
  certificates automatically:
  ```
  agent.example.com {
      reverse_proxy 127.0.0.1:8000
  }
  ```
  nginx with Let's Encrypt (certbot) also works.
- **Tunnel from a home machine**, such as Cloudflare Tunnel (`cloudflared`) or ngrok. Use a
  stable hostname. A free ngrok URL changes on every restart, and you would have to update
  `PUBLIC_BASE_URL` and the SMS webhook each time.

Signature checking requires `PUBLIC_BASE_URL` to match exactly what Twilio requests: same
scheme (`https`), same host, and any path prefix the proxy adds (for example
`https://example.com/agent` if the proxy maps `/agent/` to the app). The app rebuilds the
signed URL as `PUBLIC_BASE_URL` + route path + query string. If they don't match, every
webhook gets a 403, and the dashboard events show "bad Twilio signature".

To check the setup, text `STATUS` to your Twilio number. You should get a reply within
seconds. If you don't, look in Console → Monitor → Logs → Errors for the webhook response.

## 5. Trial-account limits

- A trial account can only call and text **verified** numbers (Console → Verified Caller
  IDs). Your own phone works once you verify it.
- Trial texts start with "Sent from your Twilio trial account". Trial calls first play a
  notice and ask you to press a key before the agent's message starts. This extra step may
  interfere with answering-machine detection and the keypad flow (verify). Upgrading removes
  both limits.

## 6. US texting rules: A2P 10DLC registration

Carriers require **A2P 10DLC registration** for application-to-person texts sent from a US
local (10-digit) number to US mobiles. That includes texts to yourself. Texts from an
unregistered number are filtered or blocked. In the Console, go to **Messaging → Regulatory
Compliance / A2P 10DLC**, register a Brand (individuals can use the *Sole Proprietor* option),
then a Campaign (a low-volume, account-notifications style use case fits). Then attach your
number to the campaign's Messaging Service.

Verify these details with Twilio's current docs:

- registration fees and monthly campaign fees
- approval time, which can take days to weeks
- exact use-case names
- whether a toll-free number with toll-free verification is the easier route for you

Calls are not affected. Until registration is approved, the agent can still phone you, but
the SMS fallback and text commands may not work reliably.

## 7. Costs and other notes (verify current pricing)

- Approval calls use answering-machine detection, so a voicemail greeting can never approve
  anything. Informational calls wait for the beep and leave a voicemail. Twilio charges a
  small extra fee per call for detection.
- Polly neural voices in `<Say>` may cost extra per character.
- Twilio handles `STOP`, `START` and (on some number types) `HELP` itself. Texting `STOP` to
  the agent's number opts you out of all its texts until you text `START` (verify how your
  number handles HELP).
- Never commit `config.env`. If the auth token leaks, rotate it in the Console and update
  `TWILIO_AUTH_TOKEN`.
