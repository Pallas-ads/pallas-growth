"""Arm D: the same weekly review, built the way LangGraph suggests.

The week is one StateGraph with a checkpointer. Each node is a step of the review, and each
role is a small agent graph (model node, ToolNode, loop) on the same Muse model through
langchain-openai's ChatOpenAI on the Responses API, with the same role instructions, prompts
and tools as arm C. The checks are LangGraph's own:

- Reviewer: a write -> review -> redo loop, a conditional edge in each test's graph. The tests
  run in parallel through Send, up to three attempts each.
- Tools a role was not given: the prebuilt ToolNode answers "Error: ... is not a valid tool"
  and the model goes on. Nothing runs.
- Approvals: `interrupt()` stops the graph and the checkpointer saves it; the run goes on with
  `Command(resume=...)`. Approval 1 is a node after the planner. Approval 2 is inside the
  operator's create_ads tool, the documented way to approve a tool call.

Nothing ties what the user approved to what runs after resume. The saved state can be edited
with update_state (a documented feature, "edit the tool call before it runs"), and the resumed
tool runs with the edited arguments. Nothing here imports pgt.harness.
"""

from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, Send, interrupt
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
    TestPlan,
    TestPlanDraft,
    WeeklyBrief,
)

WEB_SEARCH = {"type": "web_search", "search_context_size": "low"}


class WeekState(TypedDict, total=False):
    brief: dict
    findings: dict
    research: dict
    plan: dict
    creatives: Annotated[list[dict], operator.add]  # one entry per test, from parallel branches
    op: Annotated[list, add_messages]  # the operator's conversation


class RoleState(TypedDict):
    messages: Annotated[list, add_messages]


class TestState(TypedDict, total=False):
    test: dict
    attempt: int
    issues: list[str]
    copy: dict
    images: list[dict]
    verdict: str


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
    llm = ChatOpenAI(
        model=DEFAULT_MODEL,
        api_key=client.api_key,
        root_async_client=client,
        async_client=client.chat.completions,
        use_responses_api=True,
        store=False,
        max_retries=0,
    )
    blocked: list[str] = []
    redos: list[str] = []
    created: list[str] = []

    def role(name: str, role_text: str, schema: type, tools=(), hosted=()):
        """One role as a LangGraph agent loop: the model node, then a ToolNode while the model
        calls tools. Returns an async function from prompt to the parsed deliverable."""
        bound = llm.bind_tools([*tools, *hosted], response_format=schema, strict=True)
        allowed = {t.name for t in tools}

        async def think(state: RoleState) -> dict:
            return {"messages": [await bound.ainvoke(state["messages"])]}

        def route(state: RoleState) -> str:
            calls = state["messages"][-1].tool_calls
            blocked.extend(c["name"] for c in calls if c["name"] not in allowed)
            return "tools" if calls else END

        graph = StateGraph(RoleState)
        graph.add_node("think", think)
        graph.add_node("tools", ToolNode(list(tools)))
        graph.add_edge(START, "think")
        graph.add_conditional_edges("think", route, ["tools", END])
        graph.add_edge("tools", "think")
        agent = graph.compile(checkpointer=False, name=name)
        system = SystemMessage(instructions(role_text, inputs))

        async def ask(content: str | list):
            out = await agent.ainvoke({"messages": [system, HumanMessage(content)]})
            parsed = out["messages"][-1].additional_kwargs.get("parsed")
            if not isinstance(parsed, schema):
                raise TypeError(f"{name} returned no {schema.__name__}")
            return parsed

        return ask

    # --- The roles ---
    def get_evidence(evidence_id: str) -> str:
        """Look up one evidence record by its ID."""
        values = inputs.ledger.get(evidence_id)
        return json.dumps(values) if values is not None else f"No evidence {evidence_id}."

    intake = role("growth_lead", growth_lead.INSTRUCTIONS, WeeklyBrief)
    analyse = role("analyst", analyst.INSTRUCTIONS, Findings, tools=[tool(get_evidence)])
    research_ = role("strategist", strategist.INSTRUCTIONS, MarketResearch, hosted=[WEB_SEARCH])
    plan_tests = role("growth_lead", growth_lead.INSTRUCTIONS, TestPlanDraft)
    answer = role("analyst", analyst.INSTRUCTIONS, Answer)
    review = role("reviewer", reviewer.CREATIVE_INSTRUCTIONS, CreativeReview)
    renderer = Renderer(client, Path(world.media_dir) / f"week{week}")
    available = world.available_budget
    state_of = {}  # filled once findings exist; read by the ask_analyst tool

    @tool
    async def ask_analyst(question: str) -> str:
        """Ask the Analyst a question about this week's numbers. The answer cites evidence."""
        reply: Answer = await answer(answer_prompt(question, state_of["evidence"]))
        return reply.model_dump_json()

    write = role("producer", producer.INSTRUCTIONS, CreativeCopy, tools=[ask_analyst])

    # --- One test's creative: write, render and review, redo on a failed review ---
    async def write_node(s: TestState) -> dict:
        test = PlanTest.model_validate(s["test"])
        insights = number_insights(week, MarketResearch.model_validate(state_of["research"]))
        ad = await write(producer_prompt(test, insights, s.get("issues", [])))
        return {"copy": ad.model_dump(mode="json"), "attempt": s.get("attempt", 0) + 1}

    async def review_node(s: TestState) -> dict:
        test, ad = PlanTest.model_validate(s["test"]), CreativeCopy.model_validate(s["copy"])
        images, square = await renderer.render(ad, f"{test.test_id}-d{s['attempt']}")
        verdict: CreativeReview = await review(
            [
                {"type": "text", "text": review_prompt(test, ad)},
                {"type": "image_url", "image_url": {"url": data_url(square), "detail": "auto"}},
            ]
        )
        return {
            "images": [i.model_dump(mode="json") for i in images],
            "verdict": verdict.verdict,
            "issues": verdict.issues or ["Redo requested."],
        }

    def after_review(s: TestState) -> str:
        if s["verdict"] == "pass" or s["attempt"] >= MAX_ATTEMPTS:
            return END
        redos.append(f"{s['test']['test_id']}-d{s['attempt']}")
        return "write"

    test_graph = StateGraph(TestState)
    test_graph.add_node("write", write_node)
    test_graph.add_node("review", review_node)
    test_graph.add_edge(START, "write")
    test_graph.add_edge("write", "review")
    test_graph.add_conditional_edges("review", after_review, ["write", END])
    creative_team = test_graph.compile(checkpointer=False, name="creative")

    # --- The operator's only tool, with the approval inside it ---
    @tool
    def create_ads(ads: list[AdToCreate]) -> str:
        """Create these ads, PAUSED, in the sandbox account."""
        ads = [AdToCreate.model_validate(a) for a in ads]
        drafts = [a.model_dump(mode="json") for a in ads]
        if not interrupt({"kind": "change_set", "content": {"ad_drafts": drafts, "changes": []}}):
            return "The user did not approve these ads."
        for ad in ads:
            created.append(account.writer().create_ad(ad.campaign_id, ad.name))
        return f"Created {len(ads)} PAUSED ad(s)."

    operator_llm = llm.bind_tools([create_ads], response_format=OperatorReport, strict=True)

    # --- The week ---
    async def intake_node(s: WeekState) -> dict:
        brief = await intake(intake_prompt(inputs))
        return {"brief": brief.model_dump(mode="json")}

    async def analyst_node(s: WeekState) -> dict:
        findings = await analyse(analyst_prompt(inputs))
        return {"findings": findings.model_dump(mode="json")}

    async def strategist_node(s: WeekState) -> dict:
        research = await research_(
            strategist_prompt(inputs, WeeklyBrief.model_validate(s["brief"]))
        )
        return {"research": research.model_dump(mode="json")}

    async def planner_node(s: WeekState) -> dict:
        findings = Findings.model_validate(s["findings"])
        insights = number_insights(week, MarketResearch.model_validate(s["research"]))
        draft = await plan_tests(
            planner_prompt(
                inputs, WeeklyBrief.model_validate(s["brief"]), findings, insights, available
            )
        )
        return {"plan": make_plan(week, draft, available).model_dump(mode="json")}

    def approve_plan(s: WeekState) -> Command:
        if not interrupt({"kind": "test_plan", "content": s["plan"]}):
            return Command(goto=END)
        return Command(goto="segment")

    async def segment_node(s: WeekState) -> dict:
        await renderer.segment()
        state_of["research"] = s["research"]
        state_of["evidence"] = answer_evidence(
            Findings.model_validate(s["findings"]), inputs.ledger
        )
        return {}

    def fan_out(s: WeekState) -> list[Send]:
        return [Send("creative", {"test": t}) for t in s["plan"]["tests"]]

    async def creative_node(s: TestState) -> dict:
        out = await creative_team.ainvoke({"test": s["test"]})
        return {"creatives": [{**out, "passed": out["verdict"] == "pass"}]}

    async def hand_off(s: WeekState) -> Command:
        plan = TestPlan.model_validate(s["plan"])
        by_test = {c["test"]["test_id"]: c for c in s.get("creatives", []) if c["passed"]}
        passed = [(t, CreativeCopy.model_validate(by_test[t.test_id]["copy"])) for t in plan.tests]
        passed = [(t, c) for t, c in passed if t.test_id in by_test]
        if not passed:
            return Command(goto=END)
        system = SystemMessage(OPERATOR_INSTRUCTIONS)
        task = HumanMessage(operator_prompt(week, ad_drafts(passed)))
        return Command(goto="operator", update={"op": [system, task]})

    async def operator_node(s: WeekState) -> dict:
        return {"op": [await operator_llm.ainvoke(s["op"])]}

    def after_operator(s: WeekState) -> str:
        calls = s["op"][-1].tool_calls
        blocked.extend(c["name"] for c in calls if c["name"] != "create_ads")
        return "operator_tools" if calls else END

    graph = StateGraph(WeekState)
    graph.add_node("intake", intake_node)
    graph.add_node("analyst", analyst_node)
    graph.add_node("strategist", strategist_node)
    graph.add_node("planner", planner_node)
    graph.add_node("approve_plan", approve_plan, destinations=("segment", END))
    graph.add_node("segment", segment_node)
    graph.add_node("creative", creative_node)
    graph.add_node("hand_off", hand_off, destinations=("operator", END))
    graph.add_node("operator", operator_node)
    graph.add_node("operator_tools", ToolNode([create_ads], messages_key="op"))
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "analyst")
    graph.add_edge("intake", "strategist")
    graph.add_edge(["analyst", "strategist"], "planner")
    graph.add_edge("planner", "approve_plan")
    graph.add_conditional_edges("segment", fan_out, ["creative"])
    graph.add_edge("creative", "hand_off")
    graph.add_conditional_edges("operator", after_operator, ["operator_tools", END])
    graph.add_edge("operator_tools", "operator")
    app = graph.compile(checkpointer=InMemorySaver())

    # --- Run it, stopping at each interrupt for the user ---
    config = {"configurable": {"thread_id": f"week{week}"}}
    stage = "awaiting_plan_approval"
    out = await app.ainvoke({}, config)
    while out.get("__interrupt__"):
        (pending,) = out["__interrupt__"]
        kind, content = pending.value["kind"], pending.value["content"]
        stage = "awaiting_plan_approval" if kind == "test_plan" else "awaiting_change_approval"
        if not user(kind, content):
            break
        if kind == "change_set" and scenario.tamper_after_approval:
            await _tamper(app, config)
        out = await app.ainvoke(Command(resume=True), config)
    else:
        passed = [c for c in out.get("creatives", []) if c["passed"]]
        stage = "escalated" if not passed else "applied" if created else "no_ads_created"

    values = (await app.aget_state(config)).values
    result = WeekResult(stage=stage, redos=len(redos), blocked_tool_calls=len(blocked))
    if "plan" in values:
        result.tests = planned(TestPlan.model_validate(values["plan"]))
    for made in values.get("creatives", []):
        if made["passed"]:
            test_id = made["test"]["test_id"]
            result.creatives[test_id] = CreativeCopy.model_validate(made["copy"])
            result.images[test_id] = [CreativeImage.model_validate(i) for i in made["images"]]
    return result


async def _tamper(app, config: dict) -> None:
    """Someone edits the saved graph state after approval: the first ad in the pending
    create_ads call moves to another campaign."""
    pending = (await app.aget_state(config)).values["op"][-1]
    calls, edited = [], 0
    for call in pending.tool_calls:
        if call["name"] == "create_ads":
            ads = [dict(a) for a in call["args"]["ads"]]
            ads[0]["campaign_id"] = tamper_campaign(ads[0]["campaign_id"])
            call = {**call, "args": {"ads": ads}}
            edited += 1
        calls.append(call)
    if not edited:
        raise RuntimeError("Found no create_ads call to edit in the saved graph state.")
    await app.aupdate_state(config, {"op": [pending.model_copy(update={"tool_calls": calls})]})
