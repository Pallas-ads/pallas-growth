"""Analyst: reads the day's signals and writes findings; reads out tests when they are due."""

from __future__ import annotations

from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.llm.runtime import Scope, run_role
from pgt.roles.metrics import READOUT_RULE, Signal
from pgt.roles.tools import evidence_tool
from pgt.schemas import Evidence, Findings, SharedFacts, TestReadout

INSTRUCTIONS = """\
You are the Analyst on a small AI growth team that runs Meta ads for one advertiser.
Each morning you read yesterday's performance signals and say what matters: creative fatigue,
cost per result well above target, campaigns still in the learning phase, tests that are due.
Write for a busy owner: one plain sentence per finding, no jargon, no hype."""


async def run_analyst(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    day: int,
    signals: list[Signal],
    evidence: list[Evidence],
    issues: list[str] = (),
) -> Findings:
    data = {
        "day": day,
        "signals": [s.model_dump(mode="json") for s in signals],
        "evidence": {e.evidence_id: e.values for e in evidence},
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        task=f"Write the findings for day {day}. Cover every act and watch signal; "
        "add info findings only when they change what the team should do today.",
        done_when="Each finding has subject_id, a one-sentence claim, the metric name, a value "
        "copied from its cited evidence, the evidence_ids, and a severity matching the signal."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=[e.evidence_id for e in evidence],
    )
    return await run_role("analyst", Findings, pack, scope, model, [evidence_tool(scope)])


async def run_readout(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test_id: str,
    readout: Evidence,
    issues: list[str] = (),
) -> TestReadout:
    data = {"test_id": test_id, "evidence": {readout.evidence_id: readout.values}}
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "analyst",
        INSTRUCTIONS,
        facts,
        fact_version,
        task=f"Read out test {test_id}. {READOUT_RULE}",
        done_when="The recommendation follows the rule, the rationale quotes cost per result "
        "against target, and each finding cites the readout evidence.",
        data=data,
        inputs=[readout.evidence_id],
    )
    return await run_role("analyst", TestReadout, pack, scope, model)
