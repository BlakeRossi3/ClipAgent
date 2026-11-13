---
name: meal-suggestions
description: Suggest a realistic breakfast, lunch or dinner that fits what the owner likes, and learn from his feedback. Use for scheduled meal nudges and whenever he asks what to eat.
---

# Meal suggestions

## Before suggesting
- `memory_read` `Health/Meals.md` for likes, dislikes, allergies and what worked.
- `meals_suggest` returns recent suggestions; do not repeat any from the last 4 days.

## The suggestion
- One meal, specific and realistic: common ingredients, about 15 minutes for breakfast/lunch, 30 for dinner, unless he says otherwise.
- Name it and give one useful detail (time, or the one ingredient to pick up). No recipes unless asked.
- Respect allergies absolutely. Respect dislikes. Lean towards what he has accepted before, with variety.
- Breakfast before 10:00, lunch 11:30-13:30, dinner 17:30-19:30 local.

## Learning
- When he replies ("had it", "not today", "too heavy", "loved that") call `meals_record_feedback` with the status (eaten, accepted, declined) and his words.
- A clear, durable preference ("I don't eat pork", "love Thai") also goes to memory: `memory_remember` with area `meals`, confidence `stated`.
- An inferred pattern (declined three heavy dinners) may be saved with confidence `inferred`.

## Never
Moralise about food, count calories unless asked, or suggest anything he has said he dislikes.
