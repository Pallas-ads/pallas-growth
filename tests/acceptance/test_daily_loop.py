"""Acceptance scenarios 7 and 8 (PRD §11), run offline through the real client and harness."""

import asyncio

import pytest

from pgt.harness.workspace import Workspace
from pgt.llm.fake import MetaFake
from pgt.llm.meta import responses_model
from pgt.llm.offline import offline_model, offline_responder
from pgt.loops.daily import revise_day
from pgt.loops.simulate import simulate
from pgt.schemas import Change, ChangeSet, ChangeSetDraft
from pgt.schemas import TestReadout as Readout


def test_scenario_7_learning_phase_guard():
    """The Media Buyer proposes +50% on cmp_1, whose budget changed yesterday. It is blocked."""

    def scripted(body):
        if "changes" in body["text"]["format"]["schema"]["properties"]:
            return ChangeSetDraft(
                summary="Push the broad campaign harder.",
                changes=[
                    Change(
                        kind="set_budget",
                        target_id="cmp_1",
                        new_daily_budget=60,
                        reason="Scale the main campaign.",
                        evidence_ids=["ev-d1-cmp_1-state"],
                    )
                ],
            )
        return offline_responder(body)

    fake = MetaFake(scripted)
    sim = asyncio.run(simulate(1, responses_model(fake.client())))
    ws, brief = sim.ws, sim.briefs[0]

    assert brief.day == 1 and brief.review.verdict == "pass"
    assert len(brief.blocked) == 1
    reason = brief.blocked[0]
    for text in ("cmp_1", "$60", "1 days since", "minimum 3", "+50%", "20%", "learning", "$75"):
        assert text in reason, (text, reason)
    assert "blocked by house rules" in brief.headline

    record = ws.artifacts.latest("day1/change_set")
    change_set = ChangeSet.model_validate(record.content)
    failed = {r.rule for r in change_set.gate[0].results if not r.passed}
    assert failed == {
        "min_days_between_budget_changes",
        "max_budget_change",
        "learning_phase_protection",
        "daily_budget_cap",  # $60 + $15 is above the $60 cap
    }
    blocked_events = ws.events.list("policy.blocked")
    assert len(blocked_events) == 1 and set(blocked_events[0].payload["rules"]) == failed

    # Nothing was applied, and approving it anyway needs explicit overrides.
    assert sim.receipts == []
    assert sim.account.state().campaign("cmp_1").daily_budget == 40
    with pytest.raises(PermissionError):
        ws.approvals.grant("day1/change_set", record)
    assert fake.violations == []


@pytest.fixture(scope="module")
def week():
    model, fake = offline_model()
    return asyncio.run(simulate(7, model)), fake


def test_scenario_8_seven_simulated_days(week):
    sim, fake = week
    assert [b.day for b in sim.briefs] == list(range(1, 8))
    assert all(b.review.verdict == "pass" for b in sim.briefs)

    def fatigue(brief, severity):
        return [
            f
            for f in brief.findings
            if f.subject_id == "ad_1"
            and f.metric == "ctr_change_vs_first_day"
            and f.severity == severity
        ]

    # The planted fatigue is caught on day 3, not before.
    assert not fatigue(sim.briefs[0], "act") and not fatigue(sim.briefs[1], "act")
    (finding,) = fatigue(sim.briefs[2], "act")
    assert finding.value <= -0.25 and finding.evidence_ids == ["ev-d3-ad_1-ctr"]

    # Day 7 has a test readout with a valid recommendation, matching the readout rule.
    record = sim.ws.artifacts.latest("day7/readout/test_1")
    readout = Readout.model_validate(record.content)
    assert readout.recommendation in {"continue", "stop", "scale"}
    assert readout.recommendation == "scale"
    assert sim.ws.artifacts.latest("day6/readout/test_1") is None

    # Every model call went through Meta's request rules and was logged with its context hash.
    assert fake.violations == []
    calls = sim.ws.events.list("model.call")
    assert len(calls) == len(fake.requests)
    assert all(len(e.payload["context_hash"]) == 64 for e in calls)
    assert sim.ws.events.list("tool.blocked") == []


def test_week_applies_only_approved_allowed_changes(week):
    sim, _ = week
    # Day 3: swap the fatigued Streak ad for the fresh one; nothing else is applied all week.
    assert [(r.day, r.applied, r.skipped) for r in sim.receipts] == [(3, [0, 1], [])]
    state = sim.account.state()
    assert state.ad("ad_1").status == "PAUSED" and state.ad("ad_5").status == "ACTIVE"
    # Day 7: the readout says scale, but cmp_2 is still learning, so the raise is blocked.
    assert any("cmp_2" in r and "learning" in r for r in sim.briefs[6].blocked)
    assert state.campaign("cmp_2").daily_budget == 15


def test_budget_change_marks_change_set_stale(week):
    sim, _ = week
    ws = sim.ws
    facts = ws.facts.current().model_copy(update={"weekly_budget": 210.0})
    ws.facts.set(facts, "user")
    assert ws.artifacts.latest("day7/change_set").status == "stale"
    assert ws.artifacts.latest("day7/findings").status != "stale"


def test_workspace_survives_reopen(tmp_path):
    path = tmp_path / "pgt.sqlite"
    model, _ = offline_model()
    sim = asyncio.run(simulate(3, model, Workspace(path)))
    count = len(sim.ws.events.list())
    sim.ws.close()
    reopened = Workspace(path)
    assert len(reopened.events.list()) == count
    assert reopened.artifacts.latest("day3/brief") is not None
    assert reopened.approvals.is_valid("day3/change_set")
    reopened.close()


def test_send_back_has_the_media_buyer_redo_today():
    model, _ = offline_model()
    sim = asyncio.run(simulate(3, model, auto_approve=False))
    ws, account = sim.ws, sim.account
    first = ws.artifacts.latest("day3/change_set")
    assert any(g["allowed"] for g in first.content["gate"])

    asyncio.run(revise_day(ws, account, model, "Keep the budgets where they are"))
    second = ws.artifacts.latest("day3/change_set")
    assert second.version == first.version + 1
    assert ws.artifacts.get(first.artifact_id).status in ("superseded", "stale")
    assert second.content["summary"].startswith("Redone after the owner's note.")
    handoff = ws.events.list("task.handoff", loop_id="daily-d3")[-1]
    assert handoff.payload["to"] == "media_buyer" and "Keep the budgets" in handoff.payload["task"]
    request = ws.events.list("approval.requested")[-1]
    assert request.refs == [second.artifact_id]
    assert ws.artifacts.latest("day3/review").content["verdict"] == "pass"
