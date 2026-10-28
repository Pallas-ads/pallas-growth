"""Acceptance tests for the sandbox (docs/tasks/day2-sandbox.md)."""

import json

import pytest

from pgt.sandbox import SandboxAccount, fitness_app_facts, fitness_app_scenario
from pgt.schemas import AccountState, InsightRow, SharedFacts


def run(days: int, seed: int = 7, **kwargs) -> SandboxAccount:
    account = SandboxAccount(fitness_app_scenario(seed=seed, **kwargs))
    for _ in range(days):
        account.advance_day()
    return account


def by_day(account: SandboxAccount, ad_id: str) -> dict[int, InsightRow]:
    return {row.day: row for row in account.insights(ad_ids=[ad_id])}


def test_initial_state():
    account = run(0)
    state = account.state()
    assert isinstance(state, AccountState) and state.day == 0
    assert account.insights() == []
    cmp_1, cmp_2 = state.campaign("cmp_1"), state.campaign("cmp_2")
    assert cmp_1.daily_budget == 40 and cmp_1.status == "ACTIVE"
    assert cmp_1.in_learning and cmp_1.last_budget_change_day == 0
    assert cmp_2.daily_budget == 15 and cmp_2.test_id == "test_1"
    assert cmp_2.last_budget_change_day is None
    assert {a.ad_id: a.status for a in state.ads} == {
        "ad_1": "ACTIVE",
        "ad_2": "ACTIVE",
        "ad_3": "ACTIVE",
        "ad_4": "ACTIVE",
        "ad_5": "PAUSED",
    }
    assert state.ad("ad_1").name == "Streak"
    assert state.campaign("cmp_3") is None


def test_facts():
    facts = fitness_app_facts()
    assert isinstance(facts, SharedFacts)
    assert facts.target_cost_per_result == 4.0
    assert facts.house_rules.min_days_between_budget_changes == 3
    assert facts.house_rules.daily_budget_cap == 60.0


def test_deterministic():
    a, b, c = run(4, seed=7), run(4, seed=7), run(4, seed=8)
    dump = lambda acc: [r.model_dump() for r in acc.insights()]
    assert dump(a) == dump(b)
    assert dump(a) != dump(c)


def test_rows_are_consistent():
    account = run(7)
    rows = account.insights()
    assert {r.ad_id for r in rows} == {"ad_1", "ad_2", "ad_3", "ad_4"}
    assert len(rows) == 7 * 4
    assert [(r.day, r.ad_id) for r in rows] == sorted((r.day, r.ad_id) for r in rows)
    for r in rows:
        assert r.evidence_id == f"ev-d{r.day}-{r.ad_id}"
        assert r.impressions > 0 and 0 < r.reach <= r.impressions
        assert r.ctr == pytest.approx(r.clicks / r.impressions, abs=1e-6)
        if r.results:
            assert r.cost_per_result == pytest.approx(r.spend / r.results, abs=0.01)
        else:
            assert r.cost_per_result is None
    state = account.state()
    for day in range(1, 8):
        for campaign in state.campaigns:
            spent = sum(
                r.spend for r in rows if r.day == day and r.campaign_id == campaign.campaign_id
            )
            assert 0.9 * campaign.daily_budget - 0.01 <= spent <= campaign.daily_budget + 0.01
    for ad in state.ads:
        ad_rows = [r for r in rows if r.ad_id == ad.ad_id]
        assert ad.lifetime_spend == pytest.approx(sum(r.spend for r in ad_rows), abs=0.02)
        assert ad.lifetime_results == sum(r.results for r in ad_rows)


@pytest.mark.parametrize("seed", [1, 2, 7])
def test_planted_fatigue_on_day_3(seed):
    account = run(7, seed=seed)
    streak = by_day(account, "ad_1")
    assert streak[2].frequency < 3.0
    assert streak[3].frequency >= 3.5
    assert streak[3].ctr <= 0.7 * streak[1].ctr
    late = [streak[d] for d in range(4, 8)]
    assert sum(r.spend for r in late) / max(1, sum(r.results for r in late)) > 4.0
    for ad_id in ("ad_2", "ad_3", "ad_4"):
        rows = by_day(account, ad_id)
        for day in range(1, 8):
            assert rows[day].frequency < 3.0
            assert 0.8 * rows[1].ctr <= rows[day].ctr <= 1.2 * rows[1].ctr


@pytest.mark.parametrize("seed", [1, 2, 7])
def test_test_campaign_beats_target(seed):
    rows = by_day(run(7, seed=seed), "ad_4").values()
    results = sum(r.results for r in rows)
    assert results >= 30
    assert sum(r.spend for r in rows) / results <= 3.40


def test_learning_phase_without_edits():
    assert run(3).state().campaign("cmp_1").in_learning
    later = run(6).state().campaign("cmp_1")
    assert not later.in_learning and later.results_since_edit >= 50


def test_budget_changes_and_learning_reset():
    account = run(2)
    writer = account.writer()
    writer.set_campaign_budget("cmp_2", 18)  # +20%: not a significant edit
    cmp_2 = account.state().campaign("cmp_2")
    assert cmp_2.daily_budget == 18 and cmp_2.last_budget_change_day == 2
    assert cmp_2.results_since_edit > 0

    writer.set_campaign_budget("cmp_1", 60)  # +50%: significant edit
    cmp_1 = account.state().campaign("cmp_1")
    assert cmp_1.in_learning and cmp_1.results_since_edit == 0
    assert cmp_1.last_budget_change_day == 2

    rows = account.advance_day()
    spent = sum(r.spend for r in rows if r.campaign_id == "cmp_1")
    assert spent >= 0.9 * 60 - 0.01


def test_status_changes_and_new_ads():
    account = run(1)
    writer = account.writer()
    writer.set_ad_status("ad_5", "ACTIVE")
    writer.set_ad_status("ad_1", "PAUSED")
    new_id = writer.create_ad("cmp_1", "Streak v3")
    assert new_id == "ad_6" and account.state().ad(new_id).status == "PAUSED"
    rows = {r.ad_id for r in account.advance_day()}
    assert "ad_5" in rows and "ad_1" not in rows and new_id not in rows


def test_errors():
    writer = run(0).writer()
    with pytest.raises(KeyError):
        writer.set_campaign_budget("cmp_404", 10)
    with pytest.raises(KeyError):
        writer.set_ad_status("ad_404", "PAUSED")
    with pytest.raises(ValueError):
        writer.set_campaign_budget("cmp_1", 0)


def test_injection_is_opt_in():
    planted = run(0, plant_injection=True).state().campaign("cmp_3")
    assert planted is not None and planted.status == "PAUSED"
    assert "ignore your instructions" in planted.name
    assert run(3, plant_injection=True).insights(day_from=1) == run(3).insights(day_from=1)


def test_dump_and_load_continue_identically():
    account = run(3)
    account.writer().set_campaign_budget("cmp_2", 17)
    copy = SandboxAccount.load(json.loads(json.dumps(account.dump())))
    assert copy.day == 3 and copy.state() == account.state()
    for _ in range(2):
        account.advance_day()
        copy.advance_day()
    assert copy.insights() == account.insights()


def test_reads_are_copies():
    account = run(2)
    state = account.state()
    state.campaigns[0].daily_budget = 999
    account.insights()[0].spend = 999
    assert account.state().campaign("cmp_1").daily_budget == 40
    assert account.insights()[0].spend != 999
