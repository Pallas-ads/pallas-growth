"""The daily check loop (PRD §5.1): Analyst -> Reviewer -> Media Buyer -> gate -> Reviewer -> brief."""

from __future__ import annotations

from pydantic_ai.models import Model

from pgt.harness.policy import CostMeter
from pgt.harness.workspace import Workspace
from pgt.llm.runtime import Scope
from pgt.roles.analyst import run_analyst, run_readout
from pgt.roles.media_buyer import blocked_reasons, run_media_buyer, to_change_set
from pgt.roles.metrics import compute_signals
from pgt.roles.reviewer import review_changes, review_findings, review_readout, verdict
from pgt.sandbox import SandboxAccount
from pgt.schemas import DailyBrief

CHANGE_SET_FACT_KEYS = ["house_rules", "target_cost_per_result", "weekly_budget"]


async def run_daily_check(
    ws: Workspace, account: SandboxAccount, model: Model, meter: CostMeter | None = None
) -> DailyBrief:
    day = account.day
    loop_id = f"daily-d{day}"
    scope = Scope(ws, meter or CostMeter(ws.events), loop_id, day)
    facts, fact_version = ws.facts.current(), ws.facts.version
    log = ws.events.append
    log(
        "loop.started",
        "growth_lead",
        {"loop": "daily_check", "day": day},
        sim_day=day,
        loop_id=loop_id,
    )

    state = account.state()
    rows = account.insights()
    ws.ledger.add_rows(rows)
    signals, evidence = compute_signals(day, state, rows, facts)
    for item in evidence:
        ws.ledger.add(item)

    # Analyst, with one redo if the Reviewer finds problems.
    findings = await run_analyst(scope, model, facts, fact_version, day, signals, evidence)
    issues = review_findings(findings.findings, ws.ledger)
    if issues:
        log(
            "review.redo",
            "reviewer",
            {"subject": "findings", "issues": issues},
            sim_day=day,
            loop_id=loop_id,
        )
        findings = await run_analyst(
            scope, model, facts, fact_version, day, signals, evidence, issues
        )
        issues = review_findings(findings.findings, ws.ledger)
    findings_rec = ws.artifacts.publish(
        f"day{day}/findings",
        findings,
        "analyst",
        fact_version=fact_version,
        fact_keys=["target_cost_per_result"],
        sim_day=day,
        status="in_review",
    )
    upstream = [findings_rec.artifact_id]
    brief_findings = [f for f in findings.findings if f.severity != "info"]

    for signal in (s for s in signals if s.kind == "test_due"):
        evidence_id = signal.evidence_ids[0]
        readout_ev = ws.ledger.get(evidence_id)
        readout = await run_readout(
            scope, model, facts, fact_version, signal.subject_id, readout_ev
        )
        readout_issues = review_readout(readout, ws.ledger, evidence_id)
        if readout_issues:
            log(
                "review.redo",
                "reviewer",
                {"subject": "readout", "issues": readout_issues},
                sim_day=day,
                loop_id=loop_id,
            )
            readout = await run_readout(
                scope, model, facts, fact_version, signal.subject_id, readout_ev, readout_issues
            )
            readout_issues = review_readout(readout, ws.ledger, evidence_id)
        readout_rec = ws.artifacts.publish(
            f"day{day}/readout/{signal.subject_id}",
            readout,
            "analyst",
            fact_version=fact_version,
            fact_keys=["target_cost_per_result"],
            sim_day=day,
            status="in_review",
        )
        upstream.append(readout_rec.artifact_id)
        brief_findings += readout.findings
        issues += readout_issues

    change_set = None
    subject_id = findings_rec.artifact_id
    if not issues:
        # The Media Buyer acts only on findings that passed review.
        draft = await run_media_buyer(
            scope, model, facts, fact_version, state, brief_findings, upstream
        )
        issues = review_changes(draft.changes, ws.ledger)
        if issues:
            log(
                "review.redo",
                "reviewer",
                {"subject": "change_set", "issues": issues},
                sim_day=day,
                loop_id=loop_id,
            )
            draft = await run_media_buyer(
                scope, model, facts, fact_version, state, brief_findings, upstream, issues
            )
            issues = review_changes(draft.changes, ws.ledger)
        change_set = to_change_set(draft, state, facts)
        cs_rec = ws.artifacts.publish(
            f"day{day}/change_set",
            change_set,
            "media_buyer",
            inputs=upstream,
            fact_version=fact_version,
            fact_keys=CHANGE_SET_FACT_KEYS,
            sim_day=day,
            status="in_review",
        )
        subject_id = cs_rec.artifact_id
        for gate in change_set.gate:
            if not gate.allowed:
                log(
                    "policy.blocked",
                    "policy",
                    {
                        "change_set_id": change_set.change_set_id,
                        "change_index": gate.change_index,
                        "rules": [r.rule for r in gate.results if not r.passed],
                    },
                    refs=[cs_rec.artifact_id],
                    sim_day=day,
                    loop_id=loop_id,
                )

    review = verdict(subject_id, issues, final=True)
    review_rec = ws.artifacts.publish(
        f"day{day}/review", review, "reviewer", inputs=[subject_id], sim_day=day
    )
    blocked = blocked_reasons(change_set) if change_set else []
    brief = DailyBrief(
        day=day,
        headline=_headline(day, brief_findings, change_set, blocked, review.verdict),
        findings=brief_findings,
        change_set_id=change_set.change_set_id if change_set else None,
        blocked=blocked,
        review=review,
    )
    ws.artifacts.publish(
        f"day{day}/brief",
        brief,
        "growth_lead",
        inputs=[*upstream, review_rec.artifact_id],
        fact_version=fact_version,
        sim_day=day,
        status="in_review",
    )
    log(
        "loop.finished",
        "growth_lead",
        {"loop": "daily_check", "day": day, "verdict": review.verdict},
        sim_day=day,
        loop_id=loop_id,
    )
    return brief


def _headline(day, findings, change_set, blocked, review_verdict) -> str:
    act = sum(f.severity == "act" for f in findings)
    watch = sum(f.severity == "watch" for f in findings)
    if review_verdict != "pass":
        return f"Day {day}: review blocked today's work; nothing will change."
    if not change_set or not change_set.changes:
        return f"Day {day}: {act} to act on, {watch} to watch. No changes proposed."
    allowed = len(change_set.changes) - len(blocked)
    return (
        f"Day {day}: {act} to act on, {watch} to watch. "
        f"{allowed} change(s) ready for approval, {len(blocked)} blocked by house rules."
    )
