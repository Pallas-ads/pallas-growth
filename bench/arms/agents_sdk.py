"""Arm C: the same weekly review, built the way the OpenAI Agents SDK suggests.

Each role is an SDK Agent on the same Muse model, with the same role instructions, the same
tools and the same two approvals. The model setup and run config come from the Day 1 spike
(spike/variant_openai_agents.py). The checks are the SDK's own:

- Reviewer: an output guardrail on the Creative Producer. It renders the images, asks the
  reviewer agent for a verdict, and trips when the verdict is redo. The producer then runs
  again with the issues, up to three attempts.
- Tools a role was not given: `tool_not_found_behavior="return_error_to_model"`. The model gets
  an error back and nothing runs. (The default raises and ends the whole run.)
- Approval 2: the operator's `create_ads` tool has `needs_approval=True`. The run stops, the
  user sees the call, and the run resumes from the saved state. On resume the SDK checks a
  SHA-256 fingerprint of the tool name and arguments, so a call edited after approval fails.

The flow is plain Python that calls Runner.run once per role, not SDK handoffs. A handoff
passes the whole conversation on and lets the model choose the next step; the weekly review
has a fixed order and two stops for the user. The prompts and rendering are shared with
arm D (bench/arms/shared.py). Nothing here imports pgt.harness.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ModelBehaviorError,
    ModelSettings,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    ToolErrorFormatterArgs,
    WebSearchTool,
    function_tool,
    output_guardrail,
    set_tracing_disabled,
)
from agents.models.openai_responses import OpenAIResponsesModel
from agents.run_state import RunState
from pydantic_ai.models import Model

from bench.arms.shared import (
    MAX_ATTEMPTS,
    OPERATOR_INSTRUCTIONS,
    TEAM_RULES,  # noqa: F401 - re-exported for the test that keeps the rule copies equal
    AdToCreate,
    OperatorReport,
    Renderer,
    ad_drafts,
    analyst_prompt,
    answer_evidence,
    answer_prompt,
    data_url,
    instructions,
    intake_prompt,
    make_plan,
    number_insights,
    operator_prompt,
    planned,
    planner_prompt,
    producer_prompt,
    review_prompt,
    strategist_prompt,
    week_inputs,
)
from bench.scenarios import Scenario, tamper_campaign
from bench.world import ScriptedUser, WeekResult, World
from pgt.llm.fake import MetaFake
from pgt.llm.meta import DEFAULT_MODEL
from pgt.roles import analyst, growth_lead, producer, reviewer, strategist
from pgt.schemas import (
    Answer,
    CreativeCopy,
    CreativeImage,
    CreativeReview,
    Findings,
    MarketResearch,
    PlanTest,
    TestPlanDraft,
    WeeklyBrief,
)

set_tracing_disabled(True)


async def run(
    arm: str, world: World, model: Model, fake: MetaFake, user: ScriptedUser, scenario: Scenario
) -> WeekResult:
    try:
        return await _week(world, fake, user, scenario)
    except Exception as error:  # noqa: BLE001 - a crashed run is a result, not a bench error
        return WeekResult(stage=f"error: {type(error).__name__}")


async def _week(world: World, fake: MetaFake, user: ScriptedUser, scenario: Scenario) -> WeekResult:
    inputs = week_inputs(world)
    week, account = inputs.week, world.account
    client = fake.client()
    muse = OpenAIResponsesModel(DEFAULT_MODEL, client)
    settings = ModelSettings(tool_choice="auto", store=False)
    blocked: list[str] = []
    redos: list[str] = []

    def count_blocked(args: ToolErrorFormatterArgs) -> str | None:
        if args.kind == "tool_not_found":
            blocked.append(args.tool_name)
        return None  # keep the SDK's own message

    config = RunConfig(
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        tool_not_found_behavior="return_error_to_model",
        tool_error_formatter=count_blocked,
    )

    def agent(name: str, role_text: str, output_type: type, **kwargs) -> Agent:
        return Agent(
            name=name,
            instructions=instructions(role_text, inputs),
            model=muse,
            model_settings=settings,
            output_type=output_type,
            **kwargs,
        )

    async def ask(role: Agent, prompt: str | list):
        result = await Runner.run(role, prompt, run_config=config)
        return result.final_output_as(role.output_type, raise_if_incorrect_type=True)

    # --- Plan: intake, then analyst and strategist in parallel, then the test plan ---
    brief: WeeklyBrief = await ask(
        agent("growth_lead", growth_lead.INSTRUCTIONS, WeeklyBrief), intake_prompt(inputs)
    )

    @function_tool
    def get_evidence(evidence_id: str) -> str:
        """Look up one evidence record by its ID."""
        values = inputs.ledger.get(evidence_id)
        return json.dumps(values) if values is not None else f"No evidence {evidence_id}."

    findings, research = await asyncio.gather(
        ask(
            agent("analyst", analyst.INSTRUCTIONS, Findings, tools=[get_evidence]),
            analyst_prompt(inputs),
        ),
        ask(
            agent(
                "strategist",
                strategist.INSTRUCTIONS,
                MarketResearch,
                tools=[WebSearchTool(search_context_size="low")],
            ),
            strategist_prompt(inputs, brief),
        ),
    )
    insights = number_insights(week, research)
    available = world.available_budget
    draft: TestPlanDraft = await ask(
        agent("growth_lead", growth_lead.INSTRUCTIONS, TestPlanDraft),
        planner_prompt(inputs, brief, findings, insights, available),
    )
    plan = make_plan(week, draft, available)
    result = WeekResult(stage="awaiting_plan_approval", tests=planned(plan))

    # --- Approval 1: in code, before any creative work ---
    if not user("test_plan", plan.model_dump(mode="json")):
        return result

    # --- Produce: SAM once, then one producer per test with the reviewer as a guardrail ---
    renderer = Renderer(client, Path(world.media_dir) / f"week{week}")
    await renderer.segment()
    evidence_for_answers = answer_evidence(findings, inputs.ledger)
    answerer = agent("analyst", analyst.INSTRUCTIONS, Answer)
    review_agent = agent("reviewer", reviewer.CREATIVE_INSTRUCTIONS, CreativeReview)

    @function_tool
    async def ask_analyst(question: str) -> str:
        """Ask the Analyst a question about this week's numbers. The answer cites evidence."""
        answer: Answer = await ask(answerer, answer_prompt(question, evidence_for_answers))
        return answer.model_dump_json()

    def reviewer_guardrail(test: PlanTest, folder: str, made: dict):
        @output_guardrail(name="reviewer")
        async def review(ctx, agent_, ad_copy: CreativeCopy) -> GuardrailFunctionOutput:
            images, square = await renderer.render(ad_copy, folder)
            made["images"] = images
            verdict: CreativeReview = await ask(
                review_agent,
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": review_prompt(test, ad_copy)},
                            {
                                "type": "input_image",
                                "image_url": data_url(square),
                                "detail": "auto",
                            },
                        ],
                    }
                ],
            )
            return GuardrailFunctionOutput(
                output_info=verdict, tripwire_triggered=verdict.verdict != "pass"
            )

        return review

    async def produce(test: PlanTest) -> tuple[CreativeCopy, list[CreativeImage]] | None:
        issues: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            made: dict = {}
            writer = agent(
                "producer",
                producer.INSTRUCTIONS,
                CreativeCopy,
                tools=[ask_analyst],
                output_guardrails=[reviewer_guardrail(test, f"{test.test_id}-c{attempt}", made)],
            )
            try:
                ad_copy = await ask(writer, producer_prompt(test, insights, issues))
                return ad_copy, made["images"]
            except OutputGuardrailTripwireTriggered as tripped:
                verdict: CreativeReview = tripped.guardrail_result.output.output_info
                issues = verdict.issues or ["Redo requested."]
                redos.append(f"{test.test_id}-c{attempt}")
        return None

    produced = await asyncio.gather(*(produce(t) for t in plan.tests))
    passed = [(t, p) for t, p in zip(plan.tests, produced, strict=True) if p is not None]
    for test, (ad_copy, images) in passed:
        result.creatives[test.test_id] = ad_copy
        result.images[test.test_id] = images
    result.redos = len(redos)
    result.blocked_tool_calls = len(blocked)
    if not passed:
        result.stage = "escalated"
        return result

    # --- Approval 2: the SDK's tool approval on the operator's only tool ---
    created: list[str] = []

    @function_tool(needs_approval=True)
    def create_ads(ads: list[AdToCreate]) -> str:
        """Create these ads, PAUSED, in the sandbox account."""
        for ad in ads:
            created.append(account.writer().create_ad(ad.campaign_id, ad.name))
        return f"Created {len(ads)} PAUSED ad(s)."

    drafts = ad_drafts([(t, c) for t, (c, _) in passed])
    operator = Agent(
        name="operator",
        instructions=OPERATOR_INSTRUCTIONS,
        model=muse,
        model_settings=settings,
        output_type=OperatorReport,
        tools=[create_ads],
    )
    run_result = await Runner.run(operator, operator_prompt(week, drafts), run_config=config)
    result.stage = "awaiting_change_approval"
    while run_result.interruptions:
        run_state = run_result.to_state()
        for item in run_result.interruptions:
            args = json.loads(item.arguments or "{}")
            if not user("change_set", {"ad_drafts": args.get("ads", []), "changes": []}):
                return result
            run_state.approve(item)
        saved = run_state.to_json()
        if scenario.tamper_after_approval:
            _tamper(saved)
        try:
            resumed = await RunState.from_json(operator, saved)
            run_result = await Runner.run(operator, resumed, run_config=config)
        except ModelBehaviorError:
            result.stage = "refused"
            break
    else:
        result.stage = "applied" if created else "no_ads_created"
    result.blocked_tool_calls = len(blocked)
    return result


def _tamper(saved: dict) -> None:
    """Someone edits the saved run after approval: the first ad moves to another campaign."""
    edited = 0

    def walk(node) -> None:
        nonlocal edited
        if isinstance(node, dict):
            if node.get("name") == "create_ads" and isinstance(node.get("arguments"), str):
                args = json.loads(node["arguments"])
                first = args["ads"][0]
                first["campaign_id"] = tamper_campaign(first["campaign_id"])
                node["arguments"] = json.dumps(args)
                edited += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(saved)
    if not edited:
        raise RuntimeError("Found no create_ads call to edit in the saved run state.")
