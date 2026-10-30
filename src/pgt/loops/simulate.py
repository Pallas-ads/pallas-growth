"""Runs the daily check over simulated days against the sandbox account."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai.models import Model

from pgt.harness.policy import CostMeter
from pgt.harness.workspace import Workspace
from pgt.loops.daily import run_daily_check
from pgt.roles.operator import apply_change_set
from pgt.sandbox import TEMPLATES, SandboxAccount
from pgt.schemas import ChangeSet, DailyBrief, ExecutionReceipt


@dataclass
class Simulation:
    ws: Workspace
    account: SandboxAccount
    briefs: list[DailyBrief] = field(default_factory=list)
    receipts: list[ExecutionReceipt] = field(default_factory=list)


async def simulate(
    days: int,
    model: Model,
    ws: Workspace | None = None,
    seed: int = 7,
    plant_injection: bool = False,
    auto_approve: bool = True,
    meter: CostMeter | None = None,
    template: str = "app",
    account: SandboxAccount | None = None,
) -> Simulation:
    """Each day: the sandbox runs one day, then the team checks it. With `auto_approve`, the
    simulation stands in for the user and approves only the changes the gate allowed.
    Pass `account` to keep simulating an account that already has history."""
    ws = ws or Workspace()
    chosen = TEMPLATES[template]
    if account is None:
        account = SandboxAccount(chosen.scenario(seed=seed, plant_injection=plant_injection))
    if ws.facts.version == 0:
        ws.facts.set(chosen.facts(), "user")
    sim = Simulation(ws, account)
    for _ in range(days):
        account.advance_day()
        brief = await run_daily_check(ws, account, model, meter)
        sim.briefs.append(brief)
        if not (auto_approve and brief.change_set_id and brief.review.verdict == "pass"):
            continue
        key = f"day{account.day}/change_set"
        record = ws.artifacts.latest(key)
        allowed = [
            g.change_index for g in ChangeSet.model_validate(record.content).gate if g.allowed
        ]
        if not allowed:
            continue
        ws.events.append(
            "approval.simulated",
            "system",
            {
                "key": key,
                "approved": allowed,
                "note": "Simulated user approves gate-allowed changes only.",
            },
            refs=[record.artifact_id],
            sim_day=account.day,
        )
        ws.approvals.grant(key, record, actor="user", approved=allowed)
        sim.receipts.append(apply_change_set(ws, account, key))
    return sim
