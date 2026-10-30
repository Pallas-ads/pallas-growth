"""The Northpaw Supply template: an online pet store with revenue, fatigue and its own photos."""

from pgt.creative.compose import product_box
from pgt.sandbox import TEMPLATES, SandboxAccount, pet_store_scenario, product_photo


def _run(days: int) -> SandboxAccount:
    account = SandboxAccount(pet_store_scenario())
    for _ in range(days):
        account.advance_day()
    return account


def test_rows_carry_purchase_value_near_the_order_value():
    account = _run(3)
    rows = account.insights()
    assert rows and all(r.purchase_value is not None for r in rows)
    for r in rows:
        aov = account.scenario.performance[r.ad_id].avg_order_value
        assert r.purchase_value <= r.results * aov * 1.08 + 0.01


def test_fitness_rows_have_no_purchase_value():
    account = SandboxAccount(TEMPLATES["app"].scenario())
    account.advance_day()
    assert all(r.purchase_value is None for r in account.insights())


def test_small_audience_ad_tires_in_week_two():
    account = _run(14)

    def cpa(day_from, day_to):
        rows = account.insights(day_from, day_to, ["ad_1"])
        return sum(r.spend for r in rows) / sum(r.results for r in rows)

    assert cpa(1, 5) < 16 < cpa(12, 14)


def test_templates_have_their_own_name_photo_and_drafts():
    store = TEMPLATES["ecommerce"]
    scenario = store.scenario()
    assert scenario.name == store.sandbox_name == "Northpaw Supply sandbox"
    assert store.facts().result_name == "purchase"
    assert {d["name"] for d in scenario.drafts} >= {"Holiday gift guide"}
    photo = product_photo(scenario)
    assert photo is not None
    box = product_box(photo)
    # The harness sits in the middle of the studio shot, well inside the frame.
    assert 0 < box.x1 < box.x2 < box.w and 0 < box.y1 < box.y2 < box.h
    assert product_photo(TEMPLATES["app"].scenario()) is None


def test_injected_campaign_name_is_only_data():
    names = [c.name for c in pet_store_scenario(plant_injection=True).campaigns]
    assert any("ignore your instructions" in n for n in names)
