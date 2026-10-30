"""Acceptance scenario 4 (PRD §11): change a fact, and only the work built on it re-runs.

After the weekly budget drops, the test plan is re-split and the change set needs a new approval;
findings, research and the new ads are not redone, and no model is called."""

import asyncio

import pytest

from pgt.llm.media import MediaClient
from pgt.llm.offline import offline_model
from pgt.loops.refresh import refresh
from pgt.loops.simulate import simulate
from pgt.loops.weekly import apply_week, plan_week, produce_week
from pgt.schemas import TestPlan as Plan

from .test_weekly_review import approve, week_one


def set_facts(ws, account, **update):
    before = ws.facts.current()
    ws.facts.set(before.model_copy(update=update), "user")
    changed = ws.events.list("fact.changed")[-1].payload["changed"]
    return asyncio.run(refresh(ws, account, changed))


def model_calls(ws):
    return len(ws.events.list("model.call"))


def test_scenario_4_budget_drop_resplits_only_the_plan():
    ws, account, model, _ = week_one()
    asyncio.run(plan_week(ws, account, model))
    plan_v1 = ws.artifacts.latest("week1/test_plan")
    calls = model_calls(ws)

    lines = set_facts(ws, account, weekly_budget=280.0)

    assert model_calls(ws) == calls
    for key in ("week1/brief", "week1/findings", "week1/research"):
        record = ws.artifacts.latest(key)
        assert record.version == 1 and record.status != "stale", key
    plan_v2 = ws.artifacts.latest("week1/test_plan")
    assert plan_v2.version == 2 and plan_v2.status == "in_review"
    old, new = Plan.model_validate(plan_v1.content), Plan.model_validate(plan_v2.content)
    assert new.available_test_budget == 56.0
    assert sum(t.weekly_budget for t in new.tests) == pytest.approx(56.0)
    assert [t.budget_weight for t in new.tests] == [t.budget_weight for t in old.tests]
    assert new.summary == old.summary
    assert ws.events.list("approval.requested")[-1].refs == [plan_v2.artifact_id]
    assert "$77 to $56" in lines[0] and "no model was called" in lines[-1]
    done = ws.events.list("refresh.done")[-1]
    assert done.payload["changed"] == ["weekly_budget"]


def test_scenario_4_new_ads_are_reused_after_a_resplit(tmp_path):
    ws, account, model, fake = week_one()
    media = MediaClient(fake.client())
    asyncio.run(plan_week(ws, account, model))
    approve(ws, "week1/test_plan")
    asyncio.run(produce_week(ws, account, model, media, tmp_path))
    creatives = {t: ws.artifacts.latest(f"week1/creative/{t}") for t in ("w1-t1", "w1-t2")}
    cs_v1 = ws.artifacts.latest("week1/change_set")

    lines = set_facts(ws, account, weekly_budget=280.0)
    assert "Kept 2 new ads" in lines[0]
    assert ws.artifacts.get(cs_v1.artifact_id).status == "stale"
    for test_id, record in creatives.items():
        assert ws.artifacts.get(record.artifact_id).status == "in_review", test_id
    assert {e.payload["key"] for e in ws.events.list("artifact.kept")} >= {
        "week1/creative/w1-t1", "week1/review/w1-t1"}

    # The re-split plan needs a new approval; production then reuses the ads as they were.
    assert not ws.approvals.is_valid("week1/test_plan")
    approve(ws, "week1/test_plan")
    calls = model_calls(ws)
    summary = asyncio.run(produce_week(ws, account, model, media, tmp_path))
    assert summary.stage == "awaiting_change_approval"
    assert model_calls(ws) == calls  # no text model, Muse Image or SAM 3.1 call
    for test_id, record in creatives.items():
        assert ws.artifacts.latest(f"week1/creative/{test_id}").artifact_id == record.artifact_id
    assert {e.payload["creative_id"] for e in ws.events.list("creative.reused")} == {
        "w1-t1-c1", "w1-t2-c1"}
    cs_v2 = ws.artifacts.latest("week1/change_set")
    assert cs_v2.version == 2
    assert cs_v2.inputs[0] == ws.artifacts.latest("week1/test_plan").artifact_id
    assert ws.events.list("approval.requested")[-1].refs == [cs_v2.artifact_id]

    approve(ws, "week1/change_set")
    _, receipt = apply_week(ws, account)
    assert len(receipt.created_ad_ids) == 2


def test_scenario_4_house_rule_change_rechecks_todays_change_set():
    model, _ = offline_model()
    sim = asyncio.run(simulate(7, model, auto_approve=False))
    ws, account = sim.ws, sim.account
    first = ws.artifacts.latest("day7/change_set")
    assert [g["allowed"] for g in first.content["gate"]] == [True, True, False]
    calls = model_calls(ws)

    rules = ws.facts.current().house_rules.model_copy(update={"protect_learning_phase": False})
    lines = set_facts(ws, account, house_rules=rules)

    assert model_calls(ws) == calls
    second = ws.artifacts.latest("day7/change_set")
    assert second.version == 2 and [g["allowed"] for g in second.content["gate"]] == [True] * 3
    assert second.content["changes"] == first.content["changes"]
    assert ws.artifacts.latest("day7/review").content["verdict"] == "pass"
    assert ws.events.list("approval.requested")[-1].refs == [second.artifact_id]
    assert "3 of 3 changes are allowed now" in lines[0]


def test_unchanged_gate_keeps_todays_change_set():
    model, _ = offline_model()
    sim = asyncio.run(simulate(3, model, auto_approve=False))
    ws, account = sim.ws, sim.account
    record = ws.artifacts.latest("day3/change_set")
    rules = ws.facts.current().house_rules.model_copy(update={"daily_budget_cap": 90.0})
    lines = set_facts(ws, account, house_rules=rules)
    assert ws.artifacts.latest("day3/change_set").artifact_id == record.artifact_id
    assert ws.artifacts.get(record.artifact_id).status == "in_review"
    assert "same result, so it was kept" in lines[0]


def test_applied_work_is_never_marked_stale():
    model, _ = offline_model()
    sim = asyncio.run(simulate(3, model))
    ws = sim.ws
    applied = ws.artifacts.latest("day3/change_set")
    assert ws.events.list("changeset.applied")[-1].refs == [applied.artifact_id]
    set_facts(ws, sim.account, weekly_budget=280.0)
    assert ws.artifacts.get(applied.artifact_id).status != "stale"
    assert not ws.events.list("artifact.stale") or all(
        e.payload["artifact_id"] != applied.artifact_id for e in ws.events.list("artifact.stale"))
