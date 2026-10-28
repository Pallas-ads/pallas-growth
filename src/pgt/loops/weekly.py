"""The weekly growth review (PRD §5.2), in three stages split by the user's two approvals:

1. plan_week: findings -> Intake -> Analyst and Strategist in parallel -> test plan.
   Stops for approval 1 (the test plan).
2. produce_week: one Creative Producer per test (ask/answer with the Analyst, Muse Image,
   SAM 3.1, compositor) -> code checks -> model review, with up to two redos -> change set
   of new ads. Stops for approval 2 (the change set).
3. apply_week: the Operator creates the approved ads, PAUSED, in the sandbox.

Each stage refuses to run without a valid approval of the stage before it.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

from PIL import Image
from pydantic_ai.models import Model

from pgt.creative.compose import (
    cutout,
    make_demo_product_shot,
    png_bytes,
    render_creative,
    save_png,
)
from pgt.harness.creative_checks import (
    allocate_budget,
    check_copy,
    check_images,
    check_plan,
    check_research,
    extract_numbers,
)
from pgt.harness.policy import CostMeter
from pgt.harness.protocol import ask_tool, escalate, handoff
from pgt.harness.workspace import Workspace
from pgt.llm.media import MediaClient
from pgt.llm.runtime import Scope
from pgt.roles.analyst import run_answer, run_weekly_analyst
from pgt.roles.growth_lead import missing_facts, run_intake, run_plan
from pgt.roles.metrics import Signal, week_days, weekly_signals
from pgt.roles.operator import apply_change_set
from pgt.roles.producer import run_producer
from pgt.roles.reviewer import review_creative, review_findings
from pgt.roles.strategist import run_research
from pgt.sandbox import SandboxAccount
from pgt.schemas import (
    CREATIVE_SIZES,
    AdDraft,
    ChangeSet,
    CreativeCopy,
    CreativeImage,
    CreativeReview,
    CreativeSet,
    ExecutionReceipt,
    Finding,
    Findings,
    MarketResearch,
    PlanTest,
    SharedFacts,
    TestPlan,
    WeeklyReviewSummary,
)

MAX_ATTEMPTS = 3  # first draft plus two redos
PLAN_FACT_KEYS = ["weekly_budget", "test_budget_share", "target_cost_per_result"]


def current_week(account: SandboxAccount) -> int:
    return max(1, math.ceil(account.day / 7))


def _scope(ws: Workspace, account: SandboxAccount, week: int, meter: CostMeter | None) -> Scope:
    return Scope(ws, meter or CostMeter(ws.events), f"weekly-w{week}", account.day)


def _summary(scope: Scope, week: int, stage: str, **fields) -> WeeklyReviewSummary:
    escalations = [
        e.payload["reason"]
        for e in scope.ws.events.list("escalation.raised", loop_id=scope.loop_id)
    ]
    summary = WeeklyReviewSummary(
        week=week,
        stage=stage,
        plan_id=fields.get("plan_id"),
        creative_ids=fields.get("creative_ids", []),
        change_set_id=fields.get("change_set_id"),
        escalations=escalations,
    )
    scope.ws.artifacts.publish(f"week{week}/summary", summary, "growth_lead", sim_day=scope.sim_day)
    scope.ws.events.append(
        "loop.stage",
        "growth_lead",
        {"loop": "weekly_review", "week": week, "stage": stage},
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    return summary


# --- Stage 1: plan ---------------------------------------------------------------------------


def carried_signals(ws: Workspace, week: int) -> tuple[list[Signal], list[str]]:
    """Daily findings that passed review this week, as signals for the weekly Analyst, plus the
    daily headlines for the Growth Lead's Intake."""
    signals: list[Signal] = []
    headlines: list[str] = []
    readout_keys = sorted({r.key for r in ws.artifacts.list() if "/readout/" in r.key})
    for day in week_days(week):
        brief = ws.artifacts.latest(f"day{day}/brief")
        if brief:
            headlines.append(brief.content["headline"])
        review = ws.artifacts.latest(f"day{day}/review")
        if review is None or review.content["verdict"] != "pass":
            continue
        found: list[dict] = []
        record = ws.artifacts.latest(f"day{day}/findings")
        if record:
            found += [f for f in record.content["findings"] if f["severity"] == "act"]
        for key in (k for k in readout_keys if k.startswith(f"day{day}/readout/")):
            found += ws.artifacts.latest(key).content["findings"]
        for f in found:
            signals.append(
                Signal(
                    subject_id=f["subject_id"],
                    kind="carried",
                    severity=f["severity"],
                    metric=f["metric"],
                    value=f["value"],
                    evidence_ids=f["evidence_ids"],
                    note=f["claim"],
                )
            )
    return signals, headlines


def _as_findings(signals: list[Signal]) -> list[Finding]:
    return [
        Finding(
            subject_id=s.subject_id,
            claim=s.note,
            metric=s.metric,
            value=s.value,
            evidence_ids=s.evidence_ids,
            severity=s.severity,
        )
        for s in signals
    ]


async def plan_week(
    ws: Workspace,
    account: SandboxAccount,
    model: Model,
    week: int | None = None,
    meter: CostMeter | None = None,
) -> WeeklyReviewSummary:
    week = week or current_week(account)
    scope = _scope(ws, account, week, meter)
    log = ws.events.append
    facts, fv = ws.facts.current(), ws.facts.version
    log(
        "loop.started",
        "growth_lead",
        {"loop": "weekly_review", "week": week},
        sim_day=account.day,
        loop_id=scope.loop_id,
    )
    missing = missing_facts(facts)
    if missing:
        escalate(scope, "growth_lead", "Shared facts are missing: " + ", ".join(missing) + ".")
        return _summary(scope, week, "escalated")

    state = account.state()
    week_signals, evidence = weekly_signals(week, state, account.insights(), facts)
    for item in evidence:
        ws.ledger.add(item)
    carried, headlines = carried_signals(ws, week)
    signals = week_signals + carried

    # Intake from what the daily checks already reviewed; then Analyst and Strategist in parallel.
    handoff(scope, "system", "growth_lead", f"Start the week {week} review.")
    brief = await run_intake(scope, model, facts, fv, week, headlines, _as_findings(carried))
    brief_rec = ws.artifacts.publish(
        f"week{week}/brief", brief, "growth_lead", fact_version=fv, sim_day=account.day
    )
    handoff(scope, "growth_lead", "analyst", "Weekly findings", [brief_rec.artifact_id])
    handoff(scope, "growth_lead", "strategist", "Market research", [brief_rec.artifact_id])
    findings, research_run = await asyncio.gather(
        _weekly_findings(scope, model, facts, fv, week, signals, evidence),
        run_research(scope, model, facts, fv, week, brief),
    )
    findings_rec = ws.artifacts.publish(
        f"week{week}/findings",
        findings,
        "analyst",
        fact_version=fv,
        fact_keys=["target_cost_per_result"],
        sim_day=account.day,
        status="in_review",
    )
    if review_findings(findings.findings, ws.ledger):
        escalate(
            scope,
            "reviewer",
            "The weekly findings failed review twice.",
            [findings_rec.artifact_id],
        )
        return _summary(scope, week, "escalated")

    research = research_run.output
    issues = check_research(research)
    if issues:
        log(
            "review.redo",
            "reviewer",
            {"subject": "research", "by": "code", "issues": issues},
            sim_day=account.day,
            loop_id=scope.loop_id,
        )
        research_run = await run_research(scope, model, facts, fv, week, brief, issues)
        research = research_run.output
        issues = check_research(research)
    research_rec = ws.artifacts.publish(
        f"week{week}/research",
        research,
        "strategist",
        fact_version=fv,
        sim_day=account.day,
        status="in_review",
    )
    if issues:
        escalate(
            scope, "reviewer", "Market research failed review twice.", [research_rec.artifact_id]
        )
        return _summary(scope, week, "escalated")
    insights = {
        f"ins-w{week}-{i + 1}": insight.model_dump(mode="json")
        for i, insight in enumerate(research.insights)
    }

    available = round(facts.weekly_budget * facts.test_budget_share, 2)
    account_view = {
        "campaigns": [c.model_dump(mode="json") for c in state.campaigns],
        "ads": [a.model_dump(mode="json") for a in state.ads],
    }
    plan_kwargs = {
        "reviewed_evidence_ids": {i for f in findings.findings for i in f.evidence_ids},
        "insight_ids": set(insights),
        "active_campaign_ids": {c.campaign_id for c in state.campaigns if c.status == "ACTIVE"},
        "ad_ids": {a.ad_id for a in state.ads},
    }
    draft = await run_plan(
        scope, model, facts, fv, week, brief, findings.findings, insights, account_view, available
    )
    issues = check_plan(draft, **plan_kwargs)
    if issues:
        log(
            "review.redo",
            "reviewer",
            {"subject": "test_plan", "by": "code", "issues": issues},
            sim_day=account.day,
            loop_id=scope.loop_id,
        )
        draft = await run_plan(
            scope,
            model,
            facts,
            fv,
            week,
            brief,
            findings.findings,
            insights,
            account_view,
            available,
            issues,
        )
        issues = check_plan(draft, **plan_kwargs)
    if issues:
        escalate(scope, "reviewer", "The test plan failed review twice: " + " ".join(issues))
        return _summary(scope, week, "escalated")

    budgets = allocate_budget([t.budget_weight for t in draft.tests], available)
    plan = TestPlan(
        plan_id=f"week{week}/test_plan",
        week=week,
        available_test_budget=available,
        summary=draft.summary,
        tests=[
            PlanTest(**t.model_dump(), test_id=f"w{week}-t{i + 1}", weekly_budget=budget)
            for i, (t, budget) in enumerate(zip(draft.tests, budgets, strict=True))
        ],
    )
    plan_rec = ws.artifacts.publish(
        plan.plan_id,
        plan,
        "growth_lead",
        inputs=[brief_rec.artifact_id, findings_rec.artifact_id, research_rec.artifact_id],
        fact_version=fv,
        fact_keys=PLAN_FACT_KEYS,
        sim_day=account.day,
        status="in_review",
    )
    log(
        "approval.requested",
        "growth_lead",
        {"key": plan.plan_id, "what": "test plan"},
        refs=[plan_rec.artifact_id],
        sim_day=account.day,
        loop_id=scope.loop_id,
    )
    return _summary(scope, week, "awaiting_plan_approval", plan_id=plan.plan_id)


async def _weekly_findings(scope, model, facts, fv, week, signals, evidence) -> Findings:
    findings = await run_weekly_analyst(scope, model, facts, fv, week, signals, evidence)
    issues = review_findings(findings.findings, scope.ws.ledger)
    if issues:
        scope.ws.events.append(
            "review.redo",
            "reviewer",
            {"subject": "weekly_findings", "by": "code", "issues": issues},
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        findings = await run_weekly_analyst(
            scope, model, facts, fv, week, signals, evidence, issues
        )
    return findings


# --- Stage 2: produce ------------------------------------------------------------------------


def fact_numbers(facts: SharedFacts) -> list[float]:
    """Numbers ad copy may use without citing evidence: those stated in the facts' text, plus
    the target and budget. House-rule numbers are left out on purpose."""
    text = " ".join(
        [facts.product, facts.audience, facts.goal, facts.brand_voice, *facts.constraints]
    )
    return [*extract_numbers(text), facts.target_cost_per_result, facts.weekly_budget]


def _require_approval(ws: Workspace, key: str, stage: str, sim_day: int) -> None:
    if not ws.approvals.is_valid(key):
        ws.events.append(
            "stage.refused",
            "system",
            {"stage": stage, "key": key, "reason": "no valid approval"},
            sim_day=sim_day,
        )
        raise PermissionError(f"{key} has no valid approval for its current content.")


async def produce_week(
    ws: Workspace,
    account: SandboxAccount,
    model: Model,
    media: MediaClient,
    media_dir: str | Path,
    week: int | None = None,
    product_shot: Image.Image | None = None,
    meter: CostMeter | None = None,
) -> WeeklyReviewSummary:
    week = week or current_week(account)
    plan_key = f"week{week}/test_plan"
    _require_approval(ws, plan_key, "produce", account.day)
    scope = _scope(ws, account, week, meter)
    facts, fv = ws.facts.current(), ws.facts.version
    plan_rec = ws.artifacts.latest(plan_key)
    plan = TestPlan.model_validate(plan_rec.content)
    research = MarketResearch.model_validate(ws.artifacts.latest(f"week{week}/research").content)
    insights = {
        f"ins-w{week}-{i + 1}": insight.model_dump(mode="json")
        for i, insight in enumerate(research.insights)
    }
    known_urls = {s.url for i in research.insights for s in i.sources}
    findings = Findings.model_validate(ws.artifacts.latest(f"week{week}/findings").content)
    evidence_ids = sorted({i for f in findings.findings for i in f.evidence_ids})
    evidence = [ws.ledger.get(i) for i in evidence_ids if ws.ledger.has(i)]

    # The product photo is segmented once with SAM 3.1 and reused for every test.
    media_dir = Path(media_dir) / f"week{week}"
    if product_shot is None:
        product_shot, _ = make_demo_product_shot()
    save_png(product_shot, media_dir / "product_shot.png")
    try:
        boxes = await media.segment(scope, "producer", product_shot, "phone")
    except ValueError:
        escalate(scope, "producer", "SAM 3.1 found no product in the product photo.")
        return _summary(scope, week, "escalated", plan_id=plan.plan_id)
    product = cutout(product_shot, boxes[0])
    backgrounds: dict[str, Image.Image] = {}

    async def answer_for(question: str, issues: list[str]):
        return await run_answer(scope, model, facts, fv, question, evidence, issues)

    async def produce(test: PlanTest) -> CreativeSet | None:
        handoff(
            scope, "growth_lead", "producer", f"Creative for {test.test_id}", [plan_rec.artifact_id]
        )
        ask = ask_tool(scope, "producer", "analyst", test.test_id, answer_for)
        issues: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            ad_copy = await run_producer(scope, model, facts, fv, test, insights, ask, issues)
            creative_id = f"{test.test_id}-c{attempt}"
            images, square = await _render(
                scope, media, ad_copy, product, backgrounds, media_dir / creative_id
            )
            creative = CreativeSet(
                creative_id=creative_id,
                test_id=test.test_id,
                attempt=attempt,
                ad_copy=ad_copy,
                images=images,
                question_ids=_question_ids(scope, test.test_id),
            )
            creative_rec = ws.artifacts.publish(
                f"week{week}/creative/{test.test_id}",
                creative,
                "producer",
                inputs=[plan_rec.artifact_id],
                fact_version=fv,
                sim_day=account.day,
                status="in_review",
            )
            issues = check_copy(ad_copy, ws.ledger, fact_numbers(facts), known_urls)
            issues += check_images(images)
            by = "code"
            if not issues:
                review = await review_creative(scope, model, facts, fv, test, ad_copy, square)
                issues = [] if review.verdict == "pass" else review.issues or ["Redo requested."]
                by = "reviewer"
            ws.artifacts.publish(
                f"week{week}/review/{test.test_id}",
                CreativeReview(verdict="redo" if issues else "pass", issues=issues),
                "reviewer",
                inputs=[creative_rec.artifact_id],
                sim_day=account.day,
            )
            if not issues:
                return creative
            ws.events.append(
                "review.redo",
                "reviewer",
                {"subject": creative_id, "by": by, "attempt": attempt, "issues": issues},
                refs=[creative_rec.artifact_id],
                sim_day=account.day,
                loop_id=scope.loop_id,
            )
        escalate(
            scope, "reviewer", f"Creative for {test.test_id} failed review {MAX_ATTEMPTS} times."
        )
        return None

    results = await asyncio.gather(*(produce(t) for t in plan.tests))
    passed = [(t, c) for t, c in zip(plan.tests, results, strict=True) if c is not None]
    if not passed:
        return _summary(scope, week, "escalated", plan_id=plan.plan_id)

    change_set = ChangeSet(
        change_set_id=f"cs-w{week}",
        day=account.day,
        author="growth_lead",
        summary=f"Create {len(passed)} new ad(s), PAUSED, for the week {week} tests.",
        changes=[],
        gate=[],
        ad_drafts=[
            AdDraft(
                campaign_id=t.campaign_id,
                name=f"{t.test_id} {c.ad_copy.name}",
                creative_id=c.creative_id,
                test_id=t.test_id,
                evidence_ids=t.evidence_ids,
            )
            for t, c in passed
        ],
    )
    creative_ids = [c.creative_id for _, c in passed]
    cs_rec = ws.artifacts.publish(
        f"week{week}/change_set",
        change_set,
        "growth_lead",
        inputs=[
            plan_rec.artifact_id,
            *(
                ws.artifacts.latest(f"week{week}/creative/{t.test_id}").artifact_id
                for t, _ in passed
            ),
        ],
        fact_version=fv,
        sim_day=account.day,
        status="in_review",
    )
    ws.events.append(
        "approval.requested",
        "growth_lead",
        {"key": f"week{week}/change_set", "what": "new ads"},
        refs=[cs_rec.artifact_id],
        sim_day=account.day,
        loop_id=scope.loop_id,
    )
    return _summary(
        scope,
        week,
        "awaiting_change_approval",
        plan_id=plan.plan_id,
        creative_ids=creative_ids,
        change_set_id=change_set.change_set_id,
    )


async def _render(
    scope: Scope,
    media: MediaClient,
    ad_copy: CreativeCopy,
    product: Image.Image,
    backgrounds: dict[str, Image.Image],
    folder: Path,
) -> tuple[list[CreativeImage], bytes]:
    """Background from Muse Image (cached per prompt), then the three placement sizes."""
    if ad_copy.image_prompt not in backgrounds:
        backgrounds[ad_copy.image_prompt] = await media.background(
            scope, "producer", ad_copy.image_prompt
        )
    background = backgrounds[ad_copy.image_prompt]
    images, square = [], b""
    for ratio, (width, height) in CREATIVE_SIZES.items():
        rendered = render_creative(background, product, ad_copy.headline, ratio)
        path = folder / f"{ratio.replace(':', 'x')}.png"
        digest = save_png(rendered.image, path)
        images.append(
            CreativeImage(ratio=ratio, width=width, height=height, path=str(path), sha256=digest)
        )
        if ratio == "1:1":
            square = png_bytes(rendered.image)
    return images, square


def _question_ids(scope: Scope, task_id: str) -> list[str]:
    return [
        e.payload["question_id"]
        for e in scope.ws.events.list("question.answered", loop_id=scope.loop_id)
        if e.payload["task_id"] == task_id and not e.payload["issues"]
    ]


# --- Stage 3: apply --------------------------------------------------------------------------


def apply_week(
    ws: Workspace, account: SandboxAccount, week: int | None = None
) -> tuple[WeeklyReviewSummary, ExecutionReceipt]:
    week = week or current_week(account)
    key = f"week{week}/change_set"
    plan_key = f"week{week}/test_plan"
    _require_approval(ws, plan_key, "apply", account.day)
    receipt = apply_change_set(ws, account, key)
    scope = _scope(ws, account, week, None)
    change_set = ChangeSet.model_validate(ws.artifacts.latest(key).content)
    summary = _summary(
        scope,
        week,
        "applied",
        plan_id=plan_key,
        creative_ids=[d.creative_id for d in change_set.ad_drafts],
        change_set_id=change_set.change_set_id,
    )
    return summary, receipt
