"""Deterministic checks for weekly-review deliverables."""

import hashlib
import re
from collections import Counter
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image

from pgt.harness.ledger import EvidenceLedger
from pgt.harness.verify import number_matches
from pgt.schemas.weekly import (
    CREATIVE_SIZES,
    Answer,
    CreativeCopy,
    CreativeImage,
    MarketResearch,
    TestPlanDraft,
)

COPY_LIMITS: dict[str, int] = {"headline": 40, "primary_text": 125, "description": 30}

_NUMBER = re.compile(
    r"(?<![\w.#])(?P<rank>#)?"
    r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:(?P<ordinal>st|nd|rd|th)\b|(?P<scale>[kmM])(?![a-zA-Z]))?"
)


def extract_numbers(text: str) -> list[float]:
    """Read numeric claims, excluding ranks and ordinals."""
    numbers = []
    for match in _NUMBER.finditer(text):
        if match["rank"] or match["ordinal"]:
            continue
        value = float(match["value"].replace(",", ""))
        scale = match["scale"]
        if scale:
            value *= 1000 if scale == "k" else 1_000_000
        numbers.append(value)
    return numbers


def allocate_budget(weights: list[int], total: float) -> list[float]:
    """Allocate cents proportionally, leaving the rounding remainder to the last test."""
    if not weights or any(weight < 1 for weight in weights):
        raise ValueError("Budget weights must be non-empty and each at least 1.")
    amount = Decimal(str(total))
    weight_sum = sum(weights)
    parts = [
        (amount * weight / weight_sum).quantize(Decimal("0.01")) for weight in weights[:-1]
    ]
    parts.append(Decimal(str(round(total, 2))) - sum(parts, Decimal(0)))
    return [float(part) for part in parts]


def check_copy(
    ad_copy: CreativeCopy,
    ledger: EvidenceLedger,
    fact_numbers: list[float],
    known_urls: set[str],
) -> list[str]:
    """Check copy lengths, claim citations and numeric support."""
    issues = []
    for field, limit in COPY_LIMITS.items():
        length = len(getattr(ad_copy, field))
        if length > limit:
            issues.append(f"{field} is {length} characters; the limit is {limit}.")

    candidates = list(fact_numbers)
    for index, claim in enumerate(ad_copy.claims):
        if not claim.evidence_ids and not claim.source_urls:
            issues.append(f"Claim {index} has no evidence IDs or source URLs.")
        for evidence_id in claim.evidence_ids:
            evidence = ledger.get(evidence_id)
            if evidence is None:
                issues.append(f"Claim {index} cites unknown evidence {evidence_id}.")
            else:
                candidates.extend(abs(value) for value in evidence.values.values())
        for url in claim.source_urls:
            if url not in known_urls:
                issues.append(
                    f"Claim {index} cites a source URL that research did not find: {url}."
                )

    for field in COPY_LIMITS:
        for value in extract_numbers(getattr(ad_copy, field)):
            if not number_matches(value, candidates):
                issues.append(
                    f"Number {value:g} in {field} matches no cited evidence or shared fact."
                )
    return issues


def _check_image_size(ratio: str, width: int, height: int, issues: list[str]) -> None:
    expected_width, expected_height = CREATIVE_SIZES[ratio]
    if (width, height) != (expected_width, expected_height):
        issue = (
            f"Image {ratio} is {width}x{height}; "
            f"expected {expected_width}x{expected_height}."
        )
        if issue not in issues:
            issues.append(issue)


def check_images(images: list[CreativeImage]) -> list[str]:
    """Check placement coverage, dimensions and the actual local image bytes."""
    issues = []
    counts = Counter(image.ratio for image in images)
    for ratio in CREATIVE_SIZES:
        if counts[ratio] == 0:
            issues.append(f"Missing image for {ratio}.")
        elif counts[ratio] > 1:
            issues.append(f"Duplicate image for {ratio}.")

    for image in images:
        ratio = image.ratio
        _check_image_size(ratio, image.width, image.height, issues)
        try:
            data = Path(image.path).read_bytes()
        except FileNotFoundError:
            issues.append(f"Image {ratio} file is missing.")
            continue
        except OSError:
            issues.append(f"Image {ratio} file cannot be read.")
            continue

        if hashlib.sha256(data).hexdigest() != image.sha256:
            issues.append(f"Image {ratio} file does not match its sha256.")
        try:
            with Image.open(BytesIO(data)) as pixels:
                _check_image_size(ratio, *pixels.size, issues)
                pixels.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            issues.append(f"Image {ratio} file cannot be read as an image.")
    return issues


def check_plan(
    draft: TestPlanDraft,
    *,
    reviewed_evidence_ids: set[str],
    insight_ids: set[str],
    active_campaign_ids: set[str],
    ad_ids: set[str],
    products: set[str] | None = None,
    min_tests: int = 2,
    max_tests: int = 3,
) -> list[str]:
    """Check test coverage and references to this week's reviewed inputs."""
    issues = []
    count = len(draft.tests)
    if not min_tests <= count <= max_tests:
        issues.append(f"The plan has {count} test(s); it needs {min_tests} to {max_tests}.")
    if len({test.name for test in draft.tests}) != count:
        issues.append("Test names must be unique.")

    for index, test in enumerate(draft.tests):
        label = f"Test {index}"
        if not 1 <= test.budget_weight <= 5:
            issues.append(f"{label} budget_weight {test.budget_weight} is outside 1-5.")
        if not test.evidence_ids:
            issues.append(f"{label} cites no evidence.")
        for evidence_id in test.evidence_ids:
            if evidence_id not in reviewed_evidence_ids:
                issues.append(
                    f"{label} cites evidence {evidence_id} "
                    "that is not in this week's reviewed findings."
                )
        if not test.brief.insight_ids:
            issues.append(f"{label} has no insight IDs.")
        for insight_id in test.brief.insight_ids:
            if insight_id not in insight_ids:
                issues.append(f"{label} cites unknown insight {insight_id}.")
        if test.campaign_id not in active_campaign_ids:
            issues.append(f"{label} uses campaign {test.campaign_id}, which is not active.")
        replaces = test.brief.replaces_ad_id
        if replaces is not None and replaces not in ad_ids:
            issues.append(f"{label} replaces unknown ad {replaces}.")
        if products is not None and test.brief.product not in products:
            issues.append(f"{label} shows {test.brief.product!r}, which has no photo on file; "
                          f"pick one of {', '.join(sorted(products))}.")
    return issues


def check_research(research: MarketResearch) -> list[str]:
    """Require complete insights and an HTTP source for each market claim."""
    issues = []
    if not research.insights:
        issues.append("Research has no insights.")
    for index, insight in enumerate(research.insights):
        for field in ("claim", "angle", "hook"):
            if not getattr(insight, field).strip():
                issues.append(f"Insight {index} is missing {field}.")
        if not any(source.url.startswith(("http://", "https://")) for source in insight.sources):
            issues.append(f"Insight {index} has no http(s) source.")
    return issues


def check_answer(answer: Answer, ledger: EvidenceLedger) -> list[str]:
    """Check that answers cite known evidence supporting every number."""
    issues = []
    if not answer.evidence_ids:
        issues.append("The answer cites no evidence.")
    candidates = []
    for evidence_id in answer.evidence_ids:
        evidence = ledger.get(evidence_id)
        if evidence is None:
            issues.append(f"The answer cites unknown evidence {evidence_id}.")
        else:
            candidates.extend(abs(value) for value in evidence.values.values())
    for value in extract_numbers(answer.answer):
        if not number_matches(value, candidates):
            issues.append(f"Number {value:g} in the answer matches no cited evidence.")
    return issues
