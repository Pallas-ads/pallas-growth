"""Evidence existence and numeric checks for typed deliverables."""

import math

from pgt.harness.ledger import EvidenceLedger
from pgt.schemas import Change, Finding


def number_matches(value: float, candidates: list[float]) -> bool:
    return any(
        math.isclose(value, x, rel_tol=0.015, abs_tol=1e-6)
        or math.isclose(value, 100 * x, rel_tol=0.015, abs_tol=0.051)
        for x in candidates
    )


def check_findings(findings: list[Finding], ledger: EvidenceLedger) -> list[str]:
    issues = []
    for index, finding in enumerate(findings):
        label = f"Finding {index} for {finding.subject_id}"
        if not finding.evidence_ids:
            issues.append(f"{label} has no evidence IDs.")
            continue
        candidates = []
        for evidence_id in finding.evidence_ids:
            evidence = ledger.get(evidence_id)
            if evidence is None:
                issues.append(f"{label} cites unknown evidence {evidence_id}.")
            else:
                candidates.extend(evidence.values.values())
        if not number_matches(finding.value, candidates):
            issues.append(f"{label} value {finding.value:g} matches no number in its cited evidence.")
    return issues


def check_changes(changes: list[Change], ledger: EvidenceLedger) -> list[str]:
    issues = []
    for index, change in enumerate(changes):
        label = f"Change {index} for {change.target_id}"
        if not change.evidence_ids:
            issues.append(f"{label} has no evidence IDs.")
        for evidence_id in change.evidence_ids:
            if not ledger.has(evidence_id):
                issues.append(f"{label} cites unknown evidence {evidence_id}.")
    return issues
