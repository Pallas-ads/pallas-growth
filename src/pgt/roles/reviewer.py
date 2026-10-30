"""Reviewer: checks deliverables before anything reaches the user or the account.

Code checks come first (evidence, numbers, lengths, image files, the readout rule). Creative work
then gets a model review of what code cannot judge: tone, brand voice, risky claims, and whether
the image and words fit together.
"""

from __future__ import annotations

from pydantic_ai.messages import BinaryContent
from pydantic_ai.models import Model

from pgt.harness.context import assemble
from pgt.harness.ledger import EvidenceLedger
from pgt.harness.verify import check_changes, check_findings
from pgt.llm.runtime import Scope, run_role
from pgt.roles.metrics import expected_readout
from pgt.schemas import (
    Change,
    CreativeCopy,
    CreativeReview,
    Finding,
    PlanTest,
    ReviewVerdict,
    SharedFacts,
    TestReadout,
)

CREATIVE_INSTRUCTIONS = """\
You are the Reviewer on a small AI growth team that runs Meta ads for one advertiser.
Code has already checked lengths, numbers and evidence. You judge what code cannot: does the ad
follow the brand voice and every constraint in the facts, does it avoid claims that need proof
(being #1 or the best, guaranteed results, weight loss or other health outcomes), is the image
readable with the headline, and does the ad match its test brief. Ask for a redo only for a
problem the producer can fix; list each fix as one plain line.
How the image is made: Muse Image paints a background from image_prompt with no text, SAM 3.1
cuts the product named in brief.product out of the owner's real photo, and layout code places
that cutout and the ad's headline on a dark band at the top. The band and the single product
are the house layout, not faults. A claim that cites evidence IDs or a source URL has already
passed the evidence check."""


def review_findings(findings: list[Finding], ledger: EvidenceLedger) -> list[str]:
    return check_findings(findings, ledger)


def review_readout(readout: TestReadout, ledger: EvidenceLedger, evidence_id: str) -> list[str]:
    issues = check_findings(readout.findings, ledger)
    evidence = ledger.get(evidence_id)
    if evidence is None:
        return [*issues, f"Readout evidence {evidence_id} is missing."]
    expected = expected_readout(evidence.values)
    if readout.recommendation != expected:
        issues.append(
            f"Readout recommends {readout.recommendation}, but the readout rule gives {expected}."
        )
    return issues


def review_changes(changes: list[Change], ledger: EvidenceLedger) -> list[str]:
    issues = check_changes(changes, ledger)
    for index, change in enumerate(changes):
        if change.kind != "set_budget" and change.new_daily_budget is not None:
            issues.append(f"Change {index} ({change.kind}) must not set new_daily_budget.")
    return issues


def verdict(subject_id: str, issues: list[str], final: bool) -> ReviewVerdict:
    if not issues:
        return ReviewVerdict(subject_id=subject_id, verdict="pass", issues=[])
    return ReviewVerdict(subject_id=subject_id, verdict="block" if final else "redo", issues=issues)


async def review_creative(
    scope: Scope,
    model: Model,
    facts: SharedFacts,
    fact_version: int,
    test: PlanTest,
    ad_copy: CreativeCopy,
    image_png: bytes,
) -> CreativeReview:
    """Model review of one creative set. The 1:1 image is attached so the model sees it."""
    pack = assemble(
        "reviewer",
        CREATIVE_INSTRUCTIONS,
        facts,
        fact_version,
        task=f"Review the ad written for test {test.test_id} ({test.name}). The attached image "
        "is its 1:1 version.",
        done_when="verdict is pass when nothing needs fixing, with no issues; otherwise redo with "
        "one line per required fix.",
        data={"brief": test.brief.model_dump(mode="json"), "ad": ad_copy.model_dump(mode="json")},
        inputs=test.evidence_ids,
    )
    image = BinaryContent(image_png, media_type="image/png")
    return await run_role("reviewer", CreativeReview, pack, scope, model, attachments=[image])
