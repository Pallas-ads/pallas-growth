"""Reviewer: checks deliverables in code before anything reaches the user or the account.

Day 2 keeps the Reviewer deterministic (evidence, numbers, readout rule). A model-written review
of tone and claims comes with the Producer's creative work.
"""

from __future__ import annotations

from pgt.harness.ledger import EvidenceLedger
from pgt.harness.verify import check_changes, check_findings
from pgt.roles.metrics import expected_readout
from pgt.schemas import Change, Finding, ReviewVerdict, TestReadout


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
