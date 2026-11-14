---
type: agent
created: {{date}}
updated: {{date}}
source: template
confidence: stated
tags: [agent, heartbeat]
---

# Heartbeat

Every 30 minutes Sterling reads this list, looks at what is going on, and texts you (or starts a
background task) only when an item applies. **This note is yours: Sterling cannot edit it.** He may
propose a change, which you approve or reject like any other approval.

Write one item per bullet. Conditions ("If ...") are checked whenever something changes; timed
items ("Each morning", "At 9am", "Every hour", "Every Monday") run once per period.

- If a Short failed to upload, tell me
- If an approval has waited 6+ hours, remind me
- Each morning, check the weather
- If my Claude usage is above 80%, warn me
- If Sterling's nightly evals regressed, fix it
