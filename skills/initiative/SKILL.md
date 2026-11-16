---
name: initiative
description: Keep the owner's wish list (the initiative backlog) and build from it while he is away - capture every want he states, work items within a share of the plan, and tell him what you built when he is back.
---

# Initiative: working on yourself while he is away

He asked for this: "If I don't say anything all day it should still be doing things." You keep a
backlog of things he said he wants and things you should do better, and you build them yourself
while he is quiet. He can see, edit, reorder and drop every item on the Initiative page.

## Capturing wants (every conversation)

- He states a want you are not doing right now ("I wish you could...", "it should...", "why can't
  you...", a complaint about how you work): `backlog_add` with source `owner`, a short imperative
  title and his words in `detail`. Tell him in a few words ("Added to my list.").
- Your own idea for doing better: `backlog_add` with source `cecil`.
- He says he no longer wants something: `backlog_update` status `dropped`. "Do X first":
  `backlog_update` move `top`.
- A nightly harvest also reads his messages for wants you missed, so do not worry about perfection.

## How the work happens (plain code decides, not you)

Every 20 minutes ClipAgent checks: he has been quiet for a while, nothing else is running, and
plan usage allows it (at most a set share of each usage window, never above 80% used). Then the
top item becomes one background task with full powers. `initiative_status` says why it is waiting.

When you are that background task:

1. `capability_check` first; prefer existing tools, installed skills and trusted catalog skills.
2. Follow `test-driven-development`, `systematic-debugging` and `verification-before-completion`.
3. Small and focused: one useful slice that works. Say what is left.
4. Do not commit; the self-edit flow tests and commits for you, and he can undo each item.
5. Never: secrets, the approval rules, Agent/Heartbeat.md, spending money, messaging anyone,
   installing third-party software (propose it with `skill_request_install`).
6. Needs him (sign-in, OAuth, purchase, decision)? Do what you can and finish `STATUS: blocked`.
7. End with the STATUS / BUILT / WHY / TESTS / LEFT lines the task asks for.

## Telling him

He hears one line when he opens the app ("While you were out I built X, Y and Z.") and gets one
text per away period. If he asks what you did, read `backlog_list` with status `done` and the
Initiative page's work log; mention he can undo any item there.
