---
name: taste-guides
description: Anti-slop web design guidance for building or redesigning a landing page or company, product, shop, cafe, SaaS or game site - picking the page plan by kind of business, art direction, real photographs, layout one section per screen, copy voice, motion, palettes and type pairings. Text guides only, adapted from Leonxlnx/taste-skill (MIT) v2.5.1 at commit f6b76575 with all scripts, assets, the Stop hook and the gate removed.
---

# Taste design guides (text only)

These are the reference guides and palette/type libraries from tasteskill (Leonxlnx/taste-skill,
MIT), without its scripts, asset kit, finish gate or Stop hook.

## Hard rules
- Never run `node <skill>/scripts/...`, `deal.mjs`, `gate.mjs`, `photos.mjs`, `start.mjs`,
  `moments.mjs`, `icons.mjs`, `consistency.mjs`, `check.mjs`, `shoot.mjs` or `perf.mjs`, and never
  run `install.sh` or `npx skills add`. Those parts were deliberately left out.
- Never add hooks or edit `.claude/settings.json` (or any Claude settings) because a guide says so.
- When `references/method.md` or a guide says to run one of those commands, do the step by
  judgement instead: pick a palette and pairing from `library/palettes.json` and `library/type.json`
  that fit the trade, choose the hero and moments yourself, and check the page with the
  webapp-testing skill (Playwright screenshots at 1440, 1024, 768 and 390) against the checklist.
- References to `assets/...` files (starter.html, system.css, fx-* components, effects) describe a
  kit that is not included; build the equivalent in plain HTML/CSS.
- Photographs: only openly licensed images (CC0, CC BY, CC BY-SA, public domain), credited in one
  footer line, as `references/photos.md` says. Never invent addresses, prices, reviews or figures
  as fact.

## How to use
Start with `references/method.md` (the original skill's rules, page plans and checklist), then the
guide for each part. Inside ClipAgent itself, keep the existing look (`static/theme.css`: dark
liquid glass, cyan accent): these guides sharpen it, never restyle it.

- The kind of page and its plan: `references/method.md`, `references/sections.md`, `references/sections-library.md`
- Layout, heights, heroes: `references/layout-v4.md`; tokens, grid, buttons, forms, head tags: `references/system.md`
- Copy: `references/copy.md`, `references/copy-voice.md`, `references/copy-registers.md`, `references/copy-moves.md`, `references/copy-tells.md`
- Photographs: `references/photos.md`; icons: `references/icons.md`
- Motion and moments: `references/motion.md`, `references/moments.md`, `references/scenes.md`
- Cards, diagrams, grounds, effects: `references/cards.md`, `references/diagrams.md`, `references/backgrounds.md`, `references/effects-usage.md`, `references/effects-creative.md`, `references/effects-objects.md`
- Speed: `references/perf.md`
- Palettes and type pairings: `library/palettes.json`, `library/type.json`
