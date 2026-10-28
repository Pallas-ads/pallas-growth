"""Deterministic signals and derived evidence. Code does the math; models only read it."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel

from pgt.schemas import AccountState, Evidence, InsightRow, SharedFacts

TEST_LENGTH_DAYS = 7


class Signal(BaseModel):
    subject_id: str
    kind: Literal["fatigue", "high_cost", "learning", "test_due"]
    severity: Literal["info", "watch", "act"]
    metric: str
    value: float
    evidence_ids: list[str]
    note: str


def compute_signals(
    day: int, state: AccountState, rows: list[InsightRow], facts: SharedFacts
) -> tuple[list[Signal], list[Evidence]]:
    """Signals for `day`, from all insight rows up to and including it."""
    target = facts.target_cost_per_result
    by_ad: dict[str, dict[int, InsightRow]] = defaultdict(dict)
    for row in rows:
        if row.day <= day:
            by_ad[row.ad_id][row.day] = row
    signals: list[Signal] = []
    evidence: list[Evidence] = []

    for ad in state.ads:
        history = by_ad.get(ad.ad_id, {})
        today = history.get(day)
        if today is None:
            continue
        first_day = min(history)
        first = history[first_day]
        change = round(today.ctr / first.ctr - 1, 4) if first.ctr else 0.0
        ctr_id = f"ev-d{day}-{ad.ad_id}-ctr"
        evidence.append(
            Evidence(
                evidence_id=ctr_id,
                kind="derived",
                values={
                    "ctr_first_day": first.ctr,
                    "ctr_today": today.ctr,
                    "ctr_change_vs_first_day": change,
                    "frequency": today.frequency,
                },
                inputs=[first.evidence_id, today.evidence_id],
                note=f"{ad.name} ({ad.ad_id}): CTR on day {day} vs its first day (day {first_day}).",
            )
        )
        if today.frequency >= 3.5 and change <= -0.25:
            severity = "act"
        elif today.frequency >= 3.0 or change <= -0.15:
            severity = "watch"
        else:
            severity = None
        if severity:
            signals.append(
                Signal(
                    subject_id=ad.ad_id,
                    kind="fatigue",
                    severity=severity,
                    metric="ctr_change_vs_first_day",
                    value=change,
                    evidence_ids=[ctr_id],
                    note=(
                        f"{ad.name} CTR changed {change:+.0%} since day {first_day} "
                        f"at frequency {today.frequency:.2f}."
                    ),
                )
            )

        window = [history[d] for d in range(day - 2, day + 1) if d in history]
        spend = round(sum(r.spend for r in window), 2)
        results = sum(r.results for r in window)
        cpr_id = f"ev-d{day}-{ad.ad_id}-cpr3"
        values = {"spend_3d": spend, "results_3d": float(results)}
        if results:
            values["cost_per_result_3d"] = round(spend / results, 2)
        evidence.append(
            Evidence(
                evidence_id=cpr_id,
                kind="derived",
                values=values,
                inputs=[r.evidence_id for r in window],
                note=f"{ad.name} ({ad.ad_id}): last {len(window)} days, days {window[0].day}-{day}.",
            )
        )
        cpr = values.get("cost_per_result_3d")
        if spend >= 2 * target and (cpr is None or cpr > 1.5 * target):
            signals.append(
                Signal(
                    subject_id=ad.ad_id,
                    kind="high_cost",
                    severity="act",
                    metric="cost_per_result_3d" if cpr is not None else "spend_3d",
                    value=cpr if cpr is not None else spend,
                    evidence_ids=[cpr_id],
                    note=f"{ad.name} costs more than 1.5x the ${target:g} target over 3 days.",
                )
            )

    for campaign in state.campaigns:
        if campaign.status != "ACTIVE":
            continue
        state_id = f"ev-d{day}-{campaign.campaign_id}-state"
        values = {
            "daily_budget": campaign.daily_budget,
            "in_learning": 1.0 if campaign.in_learning else 0.0,
            "results_since_edit": float(campaign.results_since_edit),
        }
        if campaign.last_budget_change_day is not None:
            values["days_since_budget_change"] = float(day - campaign.last_budget_change_day)
        evidence.append(
            Evidence(
                evidence_id=state_id,
                kind="state",
                values=values,
                note=f"Campaign {campaign.campaign_id} settings at the end of day {day}.",
            )
        )
        if campaign.in_learning:
            signals.append(
                Signal(
                    subject_id=campaign.campaign_id,
                    kind="learning",
                    severity="info",
                    metric="results_since_edit",
                    value=float(campaign.results_since_edit),
                    evidence_ids=[state_id],
                    note=f"Still learning: {campaign.results_since_edit} of 50 results.",
                )
            )
        if campaign.test_id and day >= TEST_LENGTH_DAYS:
            readout = readout_evidence(day, campaign.campaign_id, campaign.test_id, rows, facts)
            evidence.append(readout)
            signals.append(
                Signal(
                    subject_id=campaign.test_id,
                    kind="test_due",
                    severity="act",
                    metric="cost_ratio_to_target",
                    value=readout.values["cost_ratio_to_target"],
                    evidence_ids=[readout.evidence_id],
                    note=f"Test {campaign.test_id} has run {day} days and is due for a readout.",
                )
            )
    return signals, evidence


def readout_evidence(
    day: int, campaign_id: str, test_id: str, rows: list[InsightRow], facts: SharedFacts
) -> Evidence:
    test_rows = [r for r in rows if r.campaign_id == campaign_id and r.day <= day]
    spend = round(sum(r.spend for r in test_rows), 2)
    results = sum(r.results for r in test_rows)
    target = facts.target_cost_per_result
    cpr = round(spend / results, 2) if results else spend
    return Evidence(
        evidence_id=f"ev-d{day}-{test_id}-readout",
        kind="derived",
        values={
            "spend": spend,
            "results": float(results),
            "cost_per_result": cpr,
            "target_cost_per_result": target,
            "cost_ratio_to_target": round(cpr / target, 3),
            "days_running": float(len({r.day for r in test_rows})),
        },
        inputs=sorted({r.evidence_id for r in test_rows}),
        note=f"Test {test_id} ({campaign_id}) totals through day {day}.",
    )


READOUT_RULE = (
    "Recommend scale when cost_per_result <= 0.85 x target and results >= 30; "
    "stop when cost_per_result > 1.2 x target and spend >= 3 x target; otherwise continue."
)


def expected_readout(values: dict[str, float]) -> str:
    """The readout rule applied in code, so the Reviewer can check the model's call."""
    target = values["target_cost_per_result"]
    if values["cost_per_result"] <= 0.85 * target and values["results"] >= 30:
        return "scale"
    if values["cost_per_result"] > 1.2 * target and values["spend"] >= 3 * target:
        return "stop"
    return "continue"
