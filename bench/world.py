"""What every arm starts from and returns, and the scripted user who approves its work."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bench.scenarios import YESTERDAY_RAISE
from pgt.harness.workspace import Workspace
from pgt.llm.offline import offline_model
from pgt.loops.simulate import simulate
from pgt.loops.weekly import current_week
from pgt.roles.metrics import weekly_signals
from pgt.sandbox import SandboxAccount
from pgt.schemas import CreativeCopy, CreativeImage, Evidence


@dataclass
class World:
    """A sandbox account after 7 simulated days of daily checks, shared by every arm. For a
    daily-check scenario, day 7 also raised one campaign's budget and day 8 has run.

    The setup always runs with the plain offline model, so the arm under test is the only
    thing that meets the misbehaving one."""

    ws: Workspace
    account: SandboxAccount
    media_dir: Path
    writes_before: int  # sandbox writes made during setup; the grader ignores these
    # This week's per-ad totals, computed once before any arm runs, so the grader can check
    # numbers in ad copy against the same evidence whichever arm wrote it.
    weekly_evidence: dict[str, Evidence] = field(default_factory=dict)

    @property
    def week(self) -> int:
        return current_week(self.account)

    @property
    def available_budget(self) -> float:
        facts = self.ws.facts.current()
        return round(facts.weekly_budget * facts.test_budget_share, 2)

    def writes(self) -> list[dict]:
        return self.account.dump()["changes"][self.writes_before :]


async def build_world(
    seed: int, plant_injection: bool, media_dir: Path, raise_yesterday: str | None = None
) -> World:
    model, _ = offline_model()
    sim = await simulate(7, model, seed=seed, plant_injection=plant_injection)
    if raise_yesterday:
        # A small raise late on day 7, within the house rules, then day 8 delivers. The arm
        # runs day 8's check, one day after the change.
        campaign = sim.account.state().campaign(raise_yesterday)
        new_budget = round(campaign.daily_budget * (1 + YESTERDAY_RAISE), 2)
        sim.account.writer().set_campaign_budget(raise_yesterday, new_budget)
        sim.account.advance_day()
    world = World(sim.ws, sim.account, media_dir, len(sim.account.dump()["changes"]))
    _, evidence = weekly_signals(
        world.week, sim.account.state(), sim.account.insights(), sim.ws.facts.current()
    )
    world.weekly_evidence = {e.evidence_id: e for e in evidence}
    return world


@dataclass
class PlannedTestResult:
    test_id: str
    campaign_id: str
    weekly_budget: float
    evidence_ids: list[str]


@dataclass
class WeekResult:
    """What an arm reports back. The grader trusts only the sandbox for what was written;
    this tells it which copy and images belong to each test. A daily check fills in only the
    stage, redos and blocked tool calls."""

    stage: str
    tests: list[PlannedTestResult] = field(default_factory=list)
    creatives: dict[str, CreativeCopy] = field(default_factory=dict)  # test_id -> final copy
    images: dict[str, list[CreativeImage]] = field(default_factory=dict)
    redos: int = 0
    blocked_tool_calls: int = 0  # tool calls the arm refused (0 for arms that cannot refuse)
    note: str = ""  # why the run stopped, when the arm can say (live results only)


@dataclass
class ScriptedUser:
    """Stands in for the user at the two approval points. It approves what it is shown, the
    way a busy owner would, and keeps a copy of exactly what that was."""

    approvals: list[tuple[str, dict]] = field(default_factory=list)

    def __call__(self, kind: str, content: dict) -> bool:
        self.approvals.append((kind, content))
        return True
