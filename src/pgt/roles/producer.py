"""Creative Producer: writes the copy for one planned test and the scene for its background.
Images are made in code from Muse Image, SAM 3.1 and the compositor."""

from __future__ import annotations

from pydantic_ai import Tool
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.llm.runtime import Scope, run_role
from pgt.schemas import CreativeCopy, PlanTest, SharedFacts

INSTRUCTIONS = """\
You are the Creative Producer on a small AI growth team that runs Meta ads for one advertiser.
You write one ad for one planned test: a headline (40 characters at most), primary text
(125 at most), a description (30 at most), and a background scene for the image. The product
photo is placed on the background in code, so the scene must have no text, no phone and no
bodies. Follow the brand voice and constraints. List every factual statement in claims with its
evidence IDs or source URLs. If you need a number from the account, ask the analyst first; use
only numbers that come back with evidence."""


async def run_producer(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test: PlanTest,
    insights: dict[str, dict],
    ask: Tool,
    issues: list[str] = (),
) -> CreativeCopy:
    data = {
        "test": test.model_dump(mode="json"),
        "insights": {i: insights[i] for i in test.brief.insight_ids if i in insights},
    }
    if issues:
        data["review_issues_to_fix"] = list(issues)
    pack = assemble(
        "producer",
        INSTRUCTIONS,
        facts,
        fact_version,
        task=f"Write the ad for test {test.test_id} ({test.name}) from its brief.",
        done_when="Lengths are within limits. Every number in the copy is backed by a claim whose "
        "evidence or source supports it. The image prompt describes a scene only."
        + (" Every review issue is fixed." if issues else ""),
        data=data,
        inputs=test.evidence_ids,
    )
    return await run_role("producer", CreativeCopy, pack, scope, model, [ask])
