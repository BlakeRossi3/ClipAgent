"""Meal ideas that learn the owner's tastes - and ask before assuming anything.

* Preferences (likes, dislikes, allergies, budget, cooking time, goals) live in the vault note
  ``Health/Meals.md`` as fact bullets; they grow from his feedback (:func:`record_feedback`) and
  his answer to the one-time preferences question (:func:`record_preferences`).
* Until that note has any facts, :func:`suggest` returns nothing and queues ONE preferences
  check-in instead of inventing a diet.
* Suggestions come from the ``quartermaster`` sub-agent (``runtime.run_task``, skill
  ``meal-suggestions``); if it is unavailable, over budget, or answers unusably (always the case
  for the offline fake), a small deterministic list is used. Meals suggested in the last
  :data:`AVOID_REPEAT_DAYS` days are not repeated; anything touching his stated dislikes or
  allergies is filtered out.
* Never medical, calorie or weight-loss advice: lines that mention it are discarded.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable, Sequence

import db
from assistant import store

NOTE = "Health/Meals.md"
AVOID_REPEAT_DAYS = 4
PREFS_KIND = "onboarding:meals"
PREFS_QUESTION = ("Before I suggest meals, {h}: anything you dislike or cannot eat, roughly what you like, "
                  "and how long you are happy to spend cooking?")

FALLBACK: dict[str, tuple[str, ...]] = {
    "breakfast": ("Scrambled eggs on toast", "Oatmeal with banana and peanut butter",
                  "Greek yogurt with granola and berries", "Breakfast burrito with eggs, cheese and salsa",
                  "Bagel with cream cheese", "Avocado toast with a fried egg", "Breakfast sandwich with egg and cheese"),
    "lunch": ("Turkey and cheese sandwich", "Chicken Caesar wrap", "Quesadilla with beans and cheese",
              "Tuna melt", "Chicken fried rice", "Grilled chicken salad", "Leftover chili with crackers"),
    "dinner": ("Sheet-pan chicken and vegetables (about 30 minutes)", "Spaghetti with meat sauce",
               "Beef tacos", "Chicken stir-fry with rice", "Salmon with rice and green beans",
               "Homemade burgers with a side salad", "Chili with cornbread"),
}
_MEDICAL = re.compile(r"\b(calorie|calories|kcal|weight[- ]?loss|lose weight|diet plan|macros|diabet|"
                      r"cholesterol|blood (?:sugar|pressure)|detox|fasting|supplement)", re.I)
_AVOID_HINT = re.compile(r"\b(allerg\w*|dislikes?|don'?t (?:eat|like)|doesn'?t (?:eat|like)|do not (?:eat|like)|"
                         r"hates?|never|avoid\w*|can'?t (?:eat|have)|cannot (?:eat|have)|no)\b", re.I)
_STOP = {"allergic", "allergy", "allergies", "dislike", "dislikes", "don't", "dont", "does", "doesn't", "like",
         "eat", "hate", "hates", "never", "avoid", "avoids", "avoiding", "cannot", "can't", "have", "with", "the",
         "and", "any", "very", "much", "food", "foods", "anything", "stated", "really", "that", "this"}
_SLOT_LINE = re.compile(r"^\s*[-*]?\s*(breakfast|lunch|dinner)\s*[:\-–]\s*(.+?)\s*$", re.I | re.M)


# ---------------------------------------------------------------------------
# preferences (vault)
# ---------------------------------------------------------------------------

def _vault():
    from assistant import vault

    vault.ensure_vault()
    return vault


def preferences() -> list[str]:
    """Fact bullets from ``Health/Meals.md`` (empty until he has told us anything)."""
    v = _vault()
    if not v.note_exists(NOTE):
        return []
    return v.facts_in(v.read_note(NOTE).body)


def avoid_words(facts: Sequence[str]) -> set[str]:
    """Words from dislike/allergy facts; any suggestion containing one is dropped (errs on the safe side)."""
    out: set[str] = set()
    for f in facts:
        if not _AVOID_HINT.search(f):
            continue
        tail = f[_AVOID_HINT.search(f).end():]  # type: ignore[union-attr]
        for w in re.findall(r"[a-z]{3,}", tail.lower()):
            if w not in _STOP:
                out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def _allowed(meal: str, avoid: set[str]) -> bool:
    low = meal.lower()
    return not _MEDICAL.search(meal) and not any(re.search(rf"\b{re.escape(w)}", low) for w in avoid)


def record_preferences(text: str, *, source: str = "sms") -> str:
    """Store his stated meal preferences (verbatim, as one fact). Returns the note path."""
    clean = " ".join((text or "").split())
    if not clean:
        raise ValueError("empty preferences")
    r = _vault().remember(f"Meal preferences (his words): {clean}", area="meals", source=source)
    db.log_event("assistant", "meal preferences recorded", data={"note": r.path, "added": r.added})
    return r.path


def request_preferences() -> int | None:
    """Queue the one-time preferences check-in (never twice). Returns the check-in id, or None."""
    from assistant.settings import get_assistant_settings

    if not db.kv_set_if_absent("assistant:meals:prefs_requested", db.now_iso()):
        return None
    h = get_assistant_settings().owner_honorific
    cid = store.add_checkin(PREFS_KIND, PREFS_QUESTION.format(h=h), db.now_iso())
    db.kv_incr(f"checkins:onboard:{PREFS_KIND.split(':', 1)[1]}")
    db.log_event("assistant", "meal preferences unknown; asking once", data={"checkin_id": cid})
    return cid


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------

def recent_meals(day: date | None = None, days: int = AVOID_REPEAT_DAYS) -> list[store.Meal]:
    d = day or db.local_today()
    return store.list_meals(since_day=d - timedelta(days=days), limit=200)


def _fallback(slot: str, day: date, avoid: set[str], recent: set[str]) -> str | None:
    options = FALLBACK[slot]
    start = day.toordinal() % len(options)
    for k in range(len(options)):
        meal = options[(start + k) % len(options)]
        if meal.lower() not in recent and _allowed(meal, avoid):
            return meal
    return None


def _from_model(slots: Sequence[str], day: date, prefs: list[str], recent: list[str]) -> dict[str, str]:
    from assistant import runtime
    from assistant.settings import get_assistant_settings

    if not get_assistant_settings().enabled and not get_assistant_settings().fake:
        return {}
    task = (f"Suggest one {', one '.join(slots)} for {day.strftime('%A %d %B')} following the meal-suggestions "
            f"skill. Do not repeat any of these recent suggestions: {'; '.join(recent) or 'none'}. "
            "Answer with exactly one line per meal in the form '<slot>: <meal, with one useful detail>' and "
            "nothing else. No health, calorie or weight advice.")
    try:
        res = runtime.run_task("quartermaster", task, channel="system", kind="meal_suggestion",
                               context={"preferences": prefs[:20]})
    except Exception as exc:  # unknown agent, registry trouble: fall back quietly
        db.log_event("assistant", "meal suggestion via quartermaster failed", level="warning",
                     data={"error": repr(exc)[:300]})
        return {}
    if res.status != "ok":
        return {}
    return {m.group(1).lower(): m.group(2).strip(" .") for m in _SLOT_LINE.finditer(res.raw_text or res.text)}


def suggest(day: date | None = None, slots: Sequence[str] = ("breakfast", "lunch", "dinner"), *,
            use_model: bool = True) -> list[dict[str, Any]]:
    """One idea per slot, recorded in ``as_meals``. Empty list (and a preferences question
    queued once) while his preferences are unknown."""
    d = day or db.local_today()
    slots = [s for s in slots if s in store.MEAL_SLOTS]
    prefs = preferences()
    if not prefs:
        request_preferences()
        return []
    avoid = avoid_words(prefs)
    recent_rows = recent_meals(d)
    recent = {m.suggestion.lower() for m in recent_rows}
    picked = _from_model(slots, d, prefs, [m.suggestion for m in recent_rows]) if use_model else {}
    out = []
    for slot in slots:
        meal = picked.get(slot)
        source = "quartermaster"
        if not meal or len(meal) > 160 or meal.lower() in recent or not _allowed(meal, avoid):
            meal, source = _fallback(slot, d, avoid, recent), "fallback"
        if not meal:
            continue
        recent.add(meal.lower())
        mid = store.add_meal(slot, meal, day=d)
        out.append({"meal_id": mid, "slot": slot, "suggestion": meal, "day": d.isoformat(), "source": source})
    return out


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------

_EATEN = re.compile(r"\b(had it|ate it|i ate|made it|cooked it|loved|delicious|tasty|was great|was good|"
                    r"enjoyed|did that|had that|eaten)\b", re.I)
_DECLINED = re.compile(r"\b(no|nah|not today|not tonight|pass|skip|don'?t|didn'?t|won'?t|too heavy|too much|"
                       r"hate|not in the mood|something else|ordered|eating out)\b", re.I)
_ACCEPTED = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|sounds good|good idea|will do|i will|i'll|great idea|"
                       r"perfect|why not)\b", re.I)
_DURABLE = re.compile(r"\b(allergic|allergy|i don'?t eat|i never eat|i do not eat|i hate|i love|i really like|"
                      r"vegetarian|vegan|halal|kosher|gluten|lactose|can'?t eat|cannot eat)\b", re.I)


def infer_status(text: str) -> str | None:
    t = text or ""
    if _EATEN.search(t):
        return "eaten"
    if _DECLINED.search(t):
        return "declined"
    if _ACCEPTED.search(t):
        return "accepted"
    return None


def record_feedback(text: str = "", *, meal_id: int | None = None, status: str | None = None,
                    source: str = "sms") -> dict[str, Any]:
    """Record his reaction to a suggestion (default: the latest one). Durable preferences in
    his words ("I don't eat pork", "allergic to shellfish") also go to ``Health/Meals.md``."""
    if meal_id is None:
        latest = store.list_meals(limit=1)
        if not latest:
            raise ValueError("no meal suggestion to give feedback on")
        meal_id = latest[0].id
    meal = store.get_meal(meal_id)
    if meal is None:
        raise ValueError(f"no meal #{meal_id}")
    st = status or infer_status(text) or ("accepted" if not text else None)
    if st is not None and st not in store.MEAL_STATUSES:
        raise ValueError(f"status must be one of {store.MEAL_STATUSES}")
    store.update_meal(meal_id, status=st, feedback=(text or None))
    remembered = None
    if text and _DURABLE.search(text):
        r = _vault().remember(text.strip(), area="meals", source=source)
        remembered = r.path if r.added else None
    try:
        _vault().append_journal(f"Meal feedback on '{meal.suggestion}' ({meal.slot}): {st or 'noted'}"
                                + (f" - \"{text.strip()[:160]}\"" if text else ""), source=source)
    except Exception:  # the journal is a record, not a dependency
        pass
    db.log_event("assistant", f"meal feedback: {st or 'noted'}", data={"meal_id": meal_id})
    return {"meal_id": meal_id, "status": st or meal.status, "remembered_in": remembered}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _tool_suggest(ctx: Any, a: dict[str, Any]) -> Any:
    """Context for choosing a meal (preferences, recent suggestions, safe fallback ideas);
    with ``chosen`` it records the suggestion the agent decided on."""
    from assistant.tools import ToolError

    slot = a.get("slot") or "dinner"
    if slot not in store.MEAL_SLOTS:
        raise ToolError(f"slot must be one of {store.MEAL_SLOTS}")
    prefs = preferences()
    if a.get("chosen"):
        meal = " ".join(str(a["chosen"]).split())[:160]
        if not _allowed(meal, avoid_words(prefs)):
            raise ToolError("that suggestion conflicts with his stated dislikes/allergies or gives health advice")
        return {"recorded_meal_id": store.add_meal(slot, meal), "slot": slot, "suggestion": meal}
    if not prefs:
        cid = request_preferences()
        return {"preferences_known": False,
                "instruction": "His meal preferences are unknown. Do not invent a diet: ask him (a preferences "
                               "question has been queued" + (f" as check-in #{cid}" if cid else "") + ")."}
    today = db.local_today()
    avoid = avoid_words(prefs)
    recent = {m.suggestion.lower() for m in recent_meals(today)}
    return {"preferences_known": True, "preferences": prefs[:30],
            "recent_suggestions": [{"day": m.day, "slot": m.slot, "suggestion": m.suggestion, "status": m.status,
                                    "feedback": m.feedback} for m in recent_meals(today)][:20],
            "fallback_ideas": [m for m in FALLBACK[slot] if m.lower() not in recent and _allowed(m, avoid)],
            "to_record": "call meals_suggest again with slot and chosen=<the meal you suggest>"}


def _tool_feedback(ctx: Any, a: dict[str, Any]) -> Any:
    from assistant.tools import ToolError

    try:
        return record_feedback(a.get("feedback") or "", meal_id=a.get("meal_id"), status=a.get("status"),
                               source=getattr(ctx, "source", "assistant") or "assistant")
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    register(Tool("meals_suggest",
                  "Meal-suggestion context: his preferences from Health/Meals.md, meals suggested in the last "
                  f"{AVOID_REPEAT_DAYS} days (do not repeat) and safe fallback ideas. Pass chosen=<meal> to record "
                  "the suggestion you make. If preferences are unknown it says so: ask him, do not invent a diet.",
                  _obj({"slot": {"type": "string", "enum": list(store.MEAL_SLOTS)}, "chosen": {"type": "string"}}),
                  _tool_suggest, owner="assistant.meals", side_effects=True))
    register(Tool("meals_record_feedback",
                  "Record his reaction to a meal suggestion (default: the latest). status: suggested, accepted, "
                  "declined or eaten (inferred from his words if omitted). Durable preferences in his words are "
                  "saved to Health/Meals.md automatically.",
                  _obj({"feedback": {"type": "string"}, "meal_id": {"type": "integer"},
                        "status": {"type": "string", "enum": list(store.MEAL_STATUSES)}}),
                  _tool_feedback, owner="assistant.meals", side_effects=True))
