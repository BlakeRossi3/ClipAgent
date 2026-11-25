---
name: explainer-shorts
description: Make whiteboard explainer Shorts (the Lemo Opuscar genre - handwriting, red doodles, eraser wipes) and upload them to their own YouTube channel, separate from Daily Orbit. Use when he asks for an explainer, a Lemo / Opuscar-style reel, or anything for the second channel.
---

# Explainer Shorts (second channel)

The Lemo Opuscar style is how we make reels for a **different YouTube account**, never the Daily Orbit
channel. Lemo Opuscar's own specs are CC BY-NC, so we do not copy them: `explainer.py` is our own
renderer for the same genre (drawn in code, no footage, no video model, no AI cost).

## Make one
1. Write a script: 4-7 scenes, each `{"title", "lines" (0-3 short), "doodle"
   (circle|underline|arrow|check|none), "say" (one or two spoken sentences)}`. Scene 1 is the hook as a
   question; the last scene answers it and says "Follow for more." Keep it under 60 seconds.
2. Save it as JSON in `renders_dir/explainers/` and run `python explainer.py <script.json>`
   (or `explainer.render(script)`). It returns the MP4 path, duration and whether it is narrated.
3. Show him the file (`show_media`) before anything else. He reviews every one.

## Upload (only after he has seen it)
- `python explainer.py upload <file.mp4> "<title>" [description]` or `explainer.upload(path, title=...)`.
- It goes to the channel named `lemo` (`explainer.EXPLAINER_CHANNEL`), **private** by default
  (`privacy="unlisted"` is the only other option). Going public is his call in YouTube Studio.
- It refuses the main channel outright; the Daily Orbit pipeline (`upload.py`) never reads this token.
- Shares the Google Cloud project's daily Data API quota with Daily Orbit (1600 units per upload).

## First time: he signs in the second account
If the upload says the token is missing, it needs him once, on this laptop:
`python youtube_auth.py --channel lemo --open-browser`, then choose the **other** Google account /
brand channel (not Daily Orbit). Check it with `python youtube_auth.py --channel lemo --check`.
Say it once, put the command on his screen, and stop; never sign in for him.
