"""Typed deliverables. Models that a role's model call returns follow Meta's strict
JSON-schema rules: no defaults, every field required, no allOf/oneOf."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Returned by model calls ---------------------------------------------------------------


class Finding(_Out):
    subject_id: str  # ad_id or campaign_id the finding is about
    claim: str  # one sentence, plain language
    metric: str  # e.g. "ctr_change_vs_baseline"
    value: float  # must match a number in one of the cited evidence entries
    evidence_ids: list[str]
    severity: Literal["info", "watch", "act"]


class Findings(_Out):
    findings: list[Finding]


ChangeKind = Literal["set_budget", "pause_ad", "activate_ad"]


class Change(_Out):
    kind: ChangeKind
    target_id: str  # campaign_id for set_budget, ad_id otherwise
    new_daily_budget: float | None  # only for set_budget
    reason: str
    evidence_ids: list[str]


class ChangeSetDraft(_Out):
    """What the Media Buyer returns. An empty change list is a valid and common answer."""

    summary: str  # what to do today and why, including why not to change things
    changes: list[Change]


class TestReadout(_Out):
    test_id: str
    recommendation: Literal["continue", "stop", "scale"]
    rationale: str
    findings: list[Finding]


# --- Built in code ---------------------------------------------------------------------------


class RuleResult(_Out):
    rule: str
    passed: bool
    reason: str


class GateResult(_Out):
    change_index: int
    allowed: bool
    results: list[RuleResult]


class AdDraft(_Out):
    """A new ad the Operator creates PAUSED after approval (weekly review only)."""

    campaign_id: str
    name: str
    creative_id: str  # the reviewed creative set it is built from
    test_id: str
    evidence_ids: list[str]


class ChangeSet(_Out):
    change_set_id: str
    day: int
    author: str
    summary: str
    changes: list[Change]
    gate: list[GateResult]  # filled by the policy gate
    ad_drafts: list[AdDraft] = []


class ReviewVerdict(_Out):
    subject_id: str  # artifact id under review
    verdict: Literal["pass", "redo", "block"]
    issues: list[str]


class ExecutionReceipt(_Out):
    change_set_id: str
    content_hash: str
    applied: list[int]  # indexes of changes that were applied
    skipped: list[int]  # indexes blocked or not approved
    day: int
    created_ad_ids: list[str] = []  # PAUSED ads created from ad_drafts
    errors: list[str] = []  # writes the account refused or failed; nothing else was retried


class DailyBrief(_Out):
    day: int
    headline: str
    findings: list[Finding]
    change_set_id: str | None
    blocked: list[str]  # human-readable reasons for changes the gate blocked
    review: ReviewVerdict | None
