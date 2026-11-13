# Twilio setup: texting and calls

**Short version:** get texting working today with **WhatsApp** (Twilio's WhatsApp Sandbox, same
Twilio account). **SMS turns on by itself** when your A2P 10DLC campaign is approved, as long as
"Send my texts by" is set to **Auto** (the default).

Why: US carriers block texts from a US local (10-digit) number until its A2P 10DLC campaign is
approved (Twilio error **30034**). Calls are not affected by A2P 10DLC.

Everything below is also in **Settings → Your phone** in the app. Leave practice mode (`DRY_RUN=true`)
on until the test messages work; in practice mode nothing is sent, only recorded on the dashboard.

## 1. The basics (once)

In Settings → Your phone: your mobile, the Twilio **Account SID** and **Auth Token** (console.twilio.com
home page, "Account info"), your **Twilio number**, and the **public web address** (step 4).
The Auth Token is also used to check that webhooks really come from Twilio (signature check).

## 2. WhatsApp today (Sandbox)

1. On the laptop, open console.twilio.com → **Messaging → Try it out → Send a WhatsApp message**
   (if the menu differs, search the console for "WhatsApp sandbox"; Twilio's docs say the sandbox
   lives in the legacy Console - verify).
2. Copy the **sandbox number** and the **join code** (the words after `join`) into
   Settings → Your phone → Texting. (Twilio's shared sandbox number is commonly +1 415 523 8886;
   use whatever your page shows.) The doctor (Settings → Twilio check) also detects the number after
   you message it and offers **Fix it**.
3. On the same page open **Sandbox settings** → **When a message comes in**:
   `https://YOUR-ADDRESS/twilio/whatsapp`, method **POST** → **Save**. Without this you can receive
   messages but your replies do not reach ClipAgent.
4. On your phone, in WhatsApp, send `join <your-code>` to the sandbox number. Twilio replies that you joined.
5. Press **Save & send me a WhatsApp test**.

Rules that matter (Twilio docs):

| Rule | What ClipAgent does |
|---|---|
| Free-form messages only within **24 hours** of your last WhatsApp message; outside it only approved templates (error 63016). | Outside the window it uses SMS if SMS works, otherwise **holds** the message and sends it the moment you write again (any message, e.g. "hi"). Held messages older than 48 h are dropped. |
| The sandbox only reaches phones that joined, and a **join lasts 3 days** (error 63015). | Holds messages; Settings shows "Not joined". Send `join <code>` again. |
| Rate limits (63018) | Nothing; ClipAgent sends few messages. |

Commands work the same on WhatsApp and SMS: `YES 12` / `NO 12` (the number is required),
`STATUS`, `PAUSE`, `RESUME`, `HELP`; anything else goes to the assistant. Photos and voice notes you
send are downloaded into `data/assistant/uploads` and handed to the assistant.

Security (both channels): Twilio signature required, only your own number is obeyed (the
`whatsapp:` prefix is stripped before comparing), each MessageSid is handled once.

### Later: a production WhatsApp sender (optional)

The sandbox is for testing. A real sender means Twilio's WhatsApp **Self Sign-up** in the console
(connect a Meta Business Portfolio, register a number that can receive Meta's verification code, possibly
your existing Twilio number), then **Meta Business Verification** (can take up to 2-4 weeks), and
approved **templates** for anything sent outside the 24-hour window. Not needed for texting yourself today.
Then put that number in "WhatsApp number". Costs and exact steps: verify on twilio.com.

## 3. SMS (A2P 10DLC)

Finish the brand + campaign in Console → Messaging → Regulatory compliance → A2P 10DLC and attach
your number to the campaign's Messaging Service. Our docs pages back the registration:
`docs/privacy.md`, `docs/terms.md`, `docs/sms-opt-in.md`. Approval takes days to weeks (verify).

- Incoming SMS webhook: Console → Phone Numbers → your number → Messaging configuration →
  "A message comes in" → `https://YOUR-ADDRESS/twilio/sms`, **HTTP POST** (the Twilio check's
  **Fix it** button can set it).
- **Auto** checks the campaign status every few hours and uses SMS once it is `VERIFIED`. If an SMS
  still fails with 30034, Auto marks SMS blocked and resends that message by WhatsApp.
- `MESSAGING_CHANNEL` in `config.env`: `auto` (default) | `sms` | `whatsapp`.

## 4. Public web address (needed for replies on either channel)

Twilio must reach this PC over https. One way on Windows is **Tailscale Funnel**: install Tailscale,
sign in, then in an **Administrator** terminal run `tailscale funnel --bg 8000`. It prints an address
like `https://your-pc.your-tailnet.ts.net`; put that in "Public web address". Funnel only serves on
public ports 443/8443/10000 (the local port 8000 is fine). Verify on tailscale.com/kb/1223 - Funnel
must be allowed for your tailnet. Cloudflare Tunnel or ngrok (a stable hostname) also work.

The address must match exactly what Twilio calls (https, host, any path prefix), or every webhook
gets 403 "bad Twilio signature" on the dashboard.

## 5. Calls

Calls use Twilio voice from your Twilio number and are not blocked by A2P 10DLC. Approval calls:
press 1 approve, 2 reject, 9 repeat; unanswered → a text "Reply YES <id> or NO <id>". On a **trial**
account calls and texts only reach verified numbers (error 21219/21608) and calls play a trial notice
first; an upgraded account has neither.

## 6. When something fails

Settings → **Twilio check** (or `python -m assistant.twilio_doctor check`, or ask Sterling - tool
`twilio_diagnose`) reads your account, number, webhook, A2P status, recent message errors and the
WhatsApp state, and explains each problem with the exact fix. Common codes:

| Code | Meaning | Fix |
|---|---|---|
| 30034 | US local number not A2P-registered: carriers block SMS | Use WhatsApp (Auto) until the campaign is approved |
| 21608 / 21219 | Trial account: number not verified | Verify your mobile or upgrade |
| 21610 | You texted STOP | Text START to the Twilio number |
| 21211 | Your number is not valid | Re-enter it as +1XXXXXXXXXX |
| 20003 | Wrong Account SID / Auth Token | Copy them again from the console |
| 11200 | Twilio could not reach the public address | Check the tunnel / Public web address |
| 63015 | Not joined to the sandbox (or the 3 days ran out) | WhatsApp `join <code>` again |
| 63016 | Outside WhatsApp's 24-hour window | Send any WhatsApp message |
| 63007 | Wrong WhatsApp From number | Use the number on the sandbox page |

Defaults that still apply on every channel: quiet hours 22:00-08:00 (non-urgent texts wait), at most
25 calls + texts per day, urgent problem calls de-duplicated for 6 hours. Never commit `config.env`;
if the Auth Token leaks, rotate it in the console.
