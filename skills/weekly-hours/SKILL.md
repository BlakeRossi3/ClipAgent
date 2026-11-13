---
name: weekly-hours
description: Ask the owner how many hours he worked this week, parse the reply, update the ledger and confirm his earnings. Use for the Friday hours check-in and for any reply that reports hours.
---

# Weekly hours

## Ask (Friday evening, or Saturday morning if quiet hours intervened)
One question, no preamble: "How many hours did you work this week, sir?"
If he has more than one active job, name them: "How many hours at the warehouse and at the cafe this week?"

## Parse the reply
- Accept "38", "38.5", "about 40", "40 hrs", "38 at the warehouse, 6 at the cafe", "same as last week", "none", "off sick".
- "about"/"around" -> use the number; note "approximate" in the ledger note.
- "same as last week" -> copy last week's hours for that job (look them up with `ledger_summary`).
- Multiple jobs -> match names case-insensitively against the job list; ask once if a name is ambiguous.
- Implausible values (over 100, negative) -> ask to confirm instead of logging.
- The week is the local Monday-to-Sunday week containing the question, unless he names another week.

## Record
Call `ledger_log_hours` once per job. Never log hours he did not state or clearly imply.

## Confirm
One SMS: hours, gross pay, and estimated take-home using the job's tax set-aside.
Example: "Logged 38 hours for this week, sir. That is $912 before tax, about $730 after the usual set-aside."
If he has a savings or income goal, add progress in a clause ("$2,400 of $5,000 this quarter").
