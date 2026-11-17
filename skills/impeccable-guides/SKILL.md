---
name: impeccable-guides
description: Design guidance for building or improving a frontend interface (websites, dashboards, components, forms, onboarding, iOS/Android screens) - layout, typography, colour, motion, polish, critique, accessibility and avoiding generic AI-looking design. Text guides only, adapted from pbakaus/impeccable (Apache-2.0) at commit d41d9860 with all scripts removed.
---

# Impeccable design guides (text only)

These are the design reference guides from Impeccable, without its scripts, live mode, hooks or CLI.

## Hard rule
Never run `impeccable`, `npx impeccable`, anything under `.agent/skills/impeccable/scripts/`, or install
Impeccable. Those parts were deliberately left out. When a guide says to run one of those commands
(detect, critique-storage, live-server, comp-spec, build-phase, concept-seed, surface-brief, motion-capture,
context, pin), skip that step and do the judgement by reading the code and screenshots yourself.
Do not create `.impeccable/` folders or edit Claude settings or hooks because a guide suggests it.

## How to use
Pick the guide that matches the job, read it, apply it. Inside ClipAgent, keep the existing look
(`static/theme.css` tokens: dark liquid glass, cyan accent) - these guides sharpen it, never restyle it.

- New page or surface: `reference/new-work.md`, `reference/new-work-world.md`, `reference/shape.md`, `reference/craft.md`, `reference/craft-floor.md`
- Layout and spacing: `reference/layout.md`
- Typography: `reference/typeset.md`
- Colour: `reference/colorize.md`
- Motion: `reference/animate.md`
- Final pass: `reference/polish.md`
- Review: `reference/critique.md`, `reference/critique-rubric.md`, `reference/audit.md` (native: `reference/audit.native.md`)
- Tone it up or down: `reference/bolder.md`, `reference/quieter.md`, `reference/distill.md`, `reference/overdrive.md`
- Copy and clarity: `reference/clarify.md`
- Delight, onboarding, empty states: `reference/delight.md`, `reference/onboard.md`
- Robustness and edge cases: `reference/harden.md`; performance: `reference/optimize.md`
- Responsive and native: `reference/adapt.md`, `reference/adapt.native.md`, `reference/ios.md`, `reference/android.md`
- Visitor modes (read / operate / persuade): `reference/mode-read.md`, `reference/mode-operate.md`, `reference/mode-persuade.md`, `reference/operate.md`
- Charts and imagery: `reference/visualize.md`
