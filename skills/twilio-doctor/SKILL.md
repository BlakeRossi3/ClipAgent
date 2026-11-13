---
name: twilio-doctor
description: Diagnose why texts or calls to the owner fail (Twilio) and tell him exactly how to fix it - credentials, number formats, webhook address, trial account, A2P 10DLC registration, carrier errors - offering safe one-click fixes.
---

# Twilio doctor

1. Call `twilio_diagnose` (or `python -m assistant.twilio_doctor check`). It is read-only.
2. Lead with the single most important problem and **its fix**, not just the cause. Examples:
   - 30034 / "Number not registered for A2P 10DLC": US carriers block texts from unregistered local
     numbers. He must finish brand + campaign registration in the Twilio console and attach the
     number to that campaign's Messaging Service, then wait for approval. If the campaign is pending,
     say there is nothing to do but wait.
   - Trial account + unverified phone (21608): verify his number under Verified Caller IDs or upgrade.
   - Webhook not ours / 11200: PUBLIC_BASE_URL must be his public https tunnel address; then fix the webhook.
   - 21610: he replied STOP; he texts START to the number.
3. Fixes you may offer (`twilio_fix`, needs his YES; the dashboard's Settings -> Twilio check has
   Fix it buttons): `set_sms_webhook` (undo: `revert_sms_webhook`), `service_use_number_webhook`,
   `normalise_numbers`. Nothing else is changed automatically - never buy numbers, change billing
   or submit registrations.
4. If texts cannot reach him at all, he will only see your answer on the dashboard; say so there.
