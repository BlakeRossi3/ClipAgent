from pathlib import Path

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


def _sent_photo(name="wa-abc.jpg", data=b"\xff\xd8food"):
    from assistant import claude_code

    p = claude_code.uploads_dir() / name
    p.write_bytes(data)
    return p


def test_food_photo_is_kept_for_good_and_logged_as_a_meal(clock):
    src = _sent_photo()
    res = meals.log_photo(src, "Chicken burrito bowl", slot="lunch")
    kept = Path(res["photo"])
    assert kept.is_relative_to(meals.food_photos_dir().resolve())
    assert kept.is_file() and kept.read_bytes() == b"\xff\xd8food"
    src.unlink()                                              # uploads clean-up cannot lose it
    assert kept.is_file()
    m = store.get_meal(res["meal_id"])
    assert (m.slot, m.suggestion, m.status, m.photo) == ("lunch", "Chicken burrito bowl", "eaten", res["photo"])
    assert [x.id for x in meals.photos()] == [m.id]


def test_food_photo_attaches_to_an_existing_meal_and_refuses_strangers(clock, tmp_path):
    mid = store.add_meal("dinner", "Beef tacos")
    res = meals.log_photo(_sent_photo("x.png"), meal_id=mid)
    assert res["meal_id"] == mid and store.get_meal(mid).photo == res["photo"]
    assert len(store.list_meals()) == 1
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        meals.log_photo(outside)
    with pytest.raises(ValueError):
        meals.log_photo(_sent_photo("notes.txt"))


def test_meals_log_photo_tool_is_registered(clock):
    from assistant import tools

    tools.load_extensions()
    t = tools.REGISTRY["meals_log_photo"]
    out = t.fn(None, {"path": str(_sent_photo("t.jpg")), "description": "Pancakes", "slot": "breakfast"})
    assert store.get_meal(out["meal_id"]).photo == out["photo"]
