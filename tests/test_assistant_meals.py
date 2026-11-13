import pytest

from assistant import meals, runtime, store, vault
from assistant_fixtures import assistant_env, fake_model  # noqa: F401

pytestmark = pytest.mark.usefixtures("assistant_env")


def test_asks_before_suggesting(clock):
    assert meals.suggest() == []
    assert meals.suggest() == []
    assert [c.kind for c in store.list_checkins()] == ["onboarding:meals"]   # asked once only


def test_suggest_filters_dislikes_and_repeats(clock, fake_model):
    meals.record_preferences("I don't eat eggs; allergic to peanuts; 20 minutes max")
    fake_model.queue_for("quartermaster", "breakfast: Scrambled eggs on toast\nlunch: Tuna melt\ndinner: Beef tacos")
    out = {m["slot"]: m for m in meals.suggest()}
    assert out["lunch"]["suggestion"] == "Tuna melt" and out["lunch"]["source"] == "quartermaster"
    assert "egg" not in out["breakfast"]["suggestion"].lower() and "peanut" not in out["breakfast"]["suggestion"].lower()
    again = meals.suggest(slots=("lunch",), use_model=False)
    assert again[0]["suggestion"] != "Tuna melt"


def test_feedback_learns(clock):
    meals.record_preferences("likes spicy food")
    mid = store.add_meal("dinner", "Beef tacos")
    res = meals.record_feedback("I don't eat beef")
    assert res["status"] == "declined" and store.get_meal(mid).feedback == "I don't eat beef"
    assert "beef" in vault.read_note(meals.NOTE).body.lower()
    assert meals._allowed("Beef tacos", meals.avoid_words(meals.preferences())) is False
    assert meals._allowed("Low-calorie weight loss bowl", set()) is False
