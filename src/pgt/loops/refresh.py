"""Re-run only what a fact change made stale (PRD §11 Scenario 4).

Changing a fact marks every deliverable that used it, and everything built on those, as stale.
This pass then redoes the cheap, mechanical part in code and keeps whatever came out the same,
the way a build system rebuilds only what changed and stops where a rebuilt input is identical:

- A pending test plan is re-split from the same tests and weights for the new budget. If the
  split is unchanged the plan is kept; otherwise the new version waits on the owner again. New
  ads whose test changed only in budget are kept, so the next production step reuses them.
- Today's pending change set is re-checked against the new house rules and facts. If the gate
  gives the same result it is kept; otherwise the new version waits on the owner.

Findings, research and creative are model work: they are never redone here. When one of them is
stale itself (a new target changes what counts as good), the summary says a new run is needed.
No model is called in this module.
"""

from __future__ import annotations

from pgt.harness import access
from pgt.harness.artifacts import ArtifactRecord
from pgt.harness.creative_checks import allocate_budget
from pgt.harness.hashing import content_hash
from pgt.harness.policy import CostMeter
from pgt.harness.protocol import handoff
from pgt.harness.workspace import Workspace
from pgt.llm.runtime import Scope
from pgt.loops.daily import CHANGE_SET_FACT_KEYS
from pgt.loops.weekly import PLAN_FACT_KEYS
from pgt.roles.media_buyer import to_change_set
from pgt.roles.reviewer import verdict
from pgt.sandbox import SandboxAccount
from pgt.schemas import ChangeSet, TestPlan
from pgt.schemas.deliverables import ChangeSetDraft


def _money(value: float) -> str:
    return f"${value:,.2f}".replace(".00", "")


def _applied(ws: Workspace) -> set[str]:
    return {e.refs[0] for e in ws.events.list("changeset.applied") if e.refs}


def _stale(record: ArtifactRecord | None) -> bool:
    return record is not None and record.status == "stale"


def _keep(ws: Workspace, record: ArtifactRecord | None, reason: str, loop_id: str) -> None:
    if _stale(record):
        ws.artifacts.keep(record.artifact_id, reason, loop_id=loop_id)


async def refresh(ws: Workspace, account: SandboxAccount, changed: list[str]) -> list[str]:
    """Redo the stale work that needs no model, log what happened, and return the summary lines."""
    lines: list[str] = []
    applied = _applied(ws)
    loop_id = f"refresh-f{ws.facts.version}"  # its own loop: no model ran here
    plans = sorted({r.key for r in ws.artifacts.list("TestPlan")},
                   key=lambda k: int(k.split("/")[0][4:]))
    for key in plans:
        lines += _refresh_week(ws, account, int(key.split("/")[0][4:]), applied, loop_id)
    lines += _refresh_day(ws, account, applied, loop_id)
    stale_model_work = [r for r in ws.artifacts.list(status="stale")
                        if r.kind in {"Findings", "MarketResearch", "CreativeSet"}
                        and r.artifact_id not in applied
                        and ws.artifacts.latest(r.key).artifact_id == r.artifact_id]
    if not lines:
        lines.append("Nothing waiting on you was built on the old values, so nothing re-ran.")
    if not stale_model_work:
        lines.append("Findings, research and new ads did not depend on what changed, so they "
                     "were not redone and no model was called.")
    ws.events.append("refresh.done", "growth_lead",
                     {"changed": changed, "fact_version": ws.facts.version, "lines": lines},
                     sim_day=account.day, loop_id=loop_id)
    return lines


def _refresh_week(ws: Workspace, account: SandboxAccount, week: int,
                  applied: set[str], loop_id: str) -> list[str]:
    plan_rec = ws.artifacts.latest(f"week{week}/test_plan")
    cs_rec = ws.artifacts.latest(f"week{week}/change_set")
    if not _stale(plan_rec) or (cs_rec and cs_rec.artifact_id in applied):
        return []
    upstream = [ws.artifacts.get(i) for i in plan_rec.inputs]
    if any(_stale(r) for r in upstream):
        return [(f"Week {week} test plan: the findings it was built on use the old values, so it "
                "needs a new weekly review.")]

    facts, fv = ws.facts.current(), ws.facts.version
    old = TestPlan.model_validate(plan_rec.content)
    available = round(facts.weekly_budget * facts.test_budget_share, 2)
    budgets = allocate_budget([t.budget_weight for t in old.tests], available)
    new = old.model_copy(update={
        "available_test_budget": available,
        "tests": [t.model_copy(update={"weekly_budget": b})
                  for t, b in zip(old.tests, budgets, strict=True)],
    })
    if content_hash(new) == plan_rec.content_hash:
        reason = "The new values leave the test budget split as it was."
        _keep(ws, plan_rec, reason, loop_id)
        for test in old.tests:
            _keep_creative(ws, week, test.test_id, reason, loop_id)
        _keep(ws, cs_rec, reason, loop_id)
        return [f"Week {week} test plan: the split comes out the same, so it was kept."]

    scope = Scope(ws, CostMeter(ws.events), loop_id, account.day)
    handoff(scope, "system", "growth_lead",
            f"Re-split the Week {week} test budget: {_money(old.available_test_budget)} becomes "
            f"{_money(available)}. Same tests, same weights.", [plan_rec.artifact_id])
    new_rec = ws.artifacts.publish(
        plan_rec.key, new, "growth_lead", inputs=list(plan_rec.inputs), fact_version=fv,
        fact_keys=PLAN_FACT_KEYS, sim_day=account.day, status="in_review",
    )
    kept = 0
    for before, after in zip(old.tests, new.tests, strict=True):
        if before.model_copy(update={"weekly_budget": 0}) == after.model_copy(
                update={"weekly_budget": 0}):
            kept += _keep_creative(ws, week, before.test_id,
                                   "Only this test's budget changed; the ad stays as it was.",
                                   loop_id)
    ws.events.append("approval.requested", "growth_lead",
                     {"key": new_rec.key, "what": "test plan"}, refs=[new_rec.artifact_id],
                     sim_day=account.day, loop_id=loop_id)
    split = ", ".join(f"{t.test_id} {_money(a.weekly_budget)} to {_money(t.weekly_budget)}"
                      for a, t in zip(old.tests, new.tests, strict=True))
    line = (f"Week {week} test plan: re-split {_money(old.available_test_budget)} to "
            f"{_money(available)} ({split}). It waits on you again.")
    if kept:
        line += (f" Kept {kept} new {'ad' if kept == 1 else 'ads'}: only the budget changed, so "
                 "the next step reuses them instead of making new ones.")
    return [line]


def _keep_creative(ws: Workspace, week: int, test_id: str, reason: str, loop_id: str) -> int:
    creative = ws.artifacts.latest(f"week{week}/creative/{test_id}")
    review = ws.artifacts.latest(f"week{week}/review/{test_id}")
    if not _stale(creative) or review is None or review.content.get("verdict") != "pass":
        return 0
    _keep(ws, creative, reason, loop_id)
    _keep(ws, review, reason, loop_id)
    return 1


def _outcome(change_set: ChangeSet) -> list[tuple[int, bool, list[str]]]:
    """What the gate decided, without the reason text (which quotes the new numbers)."""
    return [(g.change_index, g.allowed, sorted(r.rule for r in g.results if not r.passed))
            for g in change_set.gate]


def _refresh_day(ws: Workspace, account: SandboxAccount, applied: set[str],
                 loop_id: str) -> list[str]:
    day = account.day
    cs_rec = ws.artifacts.latest(f"day{day}/change_set")
    if not _stale(cs_rec) or cs_rec.artifact_id in applied:
        return []
    if any(_stale(ws.artifacts.get(i)) for i in cs_rec.inputs):
        return [(f"Day {day} change set: today's findings use the old values, so it needs a new "
                "daily check.")]
    old = ChangeSet.model_validate(cs_rec.content)
    if not old.changes:
        _keep(ws, cs_rec, "No changes to re-check.", loop_id)
        return []
    facts, fv = ws.facts.current(), ws.facts.version
    draft = ChangeSetDraft(summary=old.summary, changes=old.changes)
    new = to_change_set(draft, account.state(), facts)
    review = ws.artifacts.latest(f"day{day}/review")
    brief = ws.artifacts.latest(f"day{day}/brief")
    if _outcome(new) == _outcome(old):
        reason = "The new values give the same house-rules result."
        for record in (cs_rec, review, brief):
            _keep(ws, record, reason, loop_id)
        return [(f"Day {day} change set: re-checked against the new values; same result, so it "
                "was kept.")]

    new_rec = ws.artifacts.publish(
        cs_rec.key, new, cs_rec.author, inputs=list(cs_rec.inputs), fact_version=fv,
        fact_keys=CHANGE_SET_FACT_KEYS, sim_day=day, status="in_review",
    )
    for gate in new.gate:
        if not gate.allowed:
            ws.events.append(
                "policy.blocked", "policy",
                {"change_set_id": new.change_set_id, "change_index": gate.change_index,
                 "rules": [r.rule for r in gate.results if not r.passed]},
                refs=[new_rec.artifact_id], sim_day=day, loop_id=loop_id,
            )
    allowed = sum(g.allowed for g in new.gate)
    line = (f"Day {day} change set: re-checked against the new values; {allowed} of "
            f"{len(new.changes)} {'change is' if len(new.changes) == 1 else 'changes are'} "
            "allowed now.")
    passed = review is not None and review.content.get("verdict") == "pass"
    if passed:
        ws.artifacts.publish(f"day{day}/review", verdict(new_rec.artifact_id, [], final=True),
                             "reviewer", inputs=[new_rec.artifact_id], sim_day=day)
    if passed and allowed and access.can_propose(ws):
        ws.events.append("approval.requested", new_rec.author,
                         {"key": new_rec.key, "what": "change set"},
                         refs=[new_rec.artifact_id], sim_day=day, loop_id=loop_id)
        line += " It waits on you again."
    return [line]
