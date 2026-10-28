"""OpenAI Agents SDK per-role loops, using Meta's Responses API dialect."""

import json
from time import perf_counter

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    RunHooks,
    Runner,
    WebSearchTool,
    function_tool,
    set_tracing_disabled,
)
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)
from openai import AsyncOpenAI

from spike.common import (
    EventLog,
    Findings,
    Insights,
    Source,
    fill_sources,
    get_ad_insights,
    list_campaigns,
    policy_error,
    update_budget,
)

__all__ = ["GLUE_START", "run_analyst", "run_strategist"]
GLUE_START = "# --- glue start ---"


# --- glue start ---
set_tracing_disabled(True)


class _VisibleModel(OpenAIResponsesModel):
    def __init__(self, client, model, role, log):
        super().__init__(model, client.with_options(max_retries=0))
        self.role = role
        self.log = log
        self.sources: list[Source] = []

    async def get_response(self, *args, **kwargs):
        response = None
        started = perf_counter()
        try:
            response = await super().get_response(*args, **kwargs)
        finally:
            usage = (
                response.usage if response is not None and response.raw_usage is not None else None
            )
            self.log.add(
                "model.call",
                role=self.role,
                model=self.model,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                wall_time_s=perf_counter() - started,
            )
        for item in response.output:
            if item.type == "web_search_call":
                self.log.add(
                    "tool.call",
                    role=self.role,
                    name="web_search",
                    args=item.action.model_dump(
                        mode="json", exclude_none=True, exclude={"sources"}
                    ),
                )
            if item.type == "message":
                for content in item.content:
                    if content.type == "output_text":
                        for annotation in content.annotations:
                            if annotation.type == "url_citation":
                                self.sources.append(
                                    Source(
                                        title=annotation.title or annotation.url, url=annotation.url
                                    )
                                )
        return response


class _Hooks(RunHooks):
    def __init__(self, role: str, log: EventLog) -> None:
        self.role = role
        self.log = log

    async def on_tool_start(self, context, agent, tool):
        self.log.add(
            "tool.call",
            role=self.role,
            name=tool.name,
            args=json.loads(context.tool_arguments),
        )


async def _run(client, log, model, role, output_type, instructions, tools=(), search=False):
    @tool_input_guardrail
    def allowlist(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if error := policy_error(role, data.context.tool_name, log):
            return ToolGuardrailFunctionOutput.reject_content(error)
        return ToolGuardrailFunctionOutput.allow()

    registered = [function_tool(tool, tool_input_guardrails=[allowlist]) for tool in tools]
    # Local tool hooks don't cover hosted tools. Gate exposure, then observe model output.
    if search and policy_error(role, "web_search", log) is None:
        registered.append(WebSearchTool())
    responses = _VisibleModel(client, model, role, log)
    agent = Agent(
        name=role,
        instructions=instructions,
        model=responses,
        model_settings=ModelSettings(tool_choice="auto", store=False, preserve_raw_usage=True),
        output_type=output_type,
        tools=registered,
    )
    result = await Runner.run(
        agent,
        "Complete your assigned task and return the typed deliverable.",
        hooks=_Hooks(role, log),
        run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
    )
    output = result.final_output_as(output_type, raise_if_incorrect_type=True)
    return fill_sources(output, responses.sources) if isinstance(output, Insights) else output


# --- glue end ---


async def run_analyst(
    client: AsyncOpenAI, log: EventLog, model: str = "muse-spark-1.3"
) -> Findings:
    return await _run(
        client,
        log,
        model,
        "analyst",
        Findings,
        "Find the worst-performing ad in the synthetic fitness-app campaign. "
        "List campaigns and inspect each ad's weekly insights. Use the metrics already computed "
        "by the tools; do not calculate numbers. Cite evidence IDs for every finding.",
        tools=[list_campaigns, get_ad_insights, update_budget],
    )


async def run_strategist(
    client: AsyncOpenAI, log: EventLog, model: str = "muse-spark-1.3"
) -> Insights:
    return await _run(
        client,
        log,
        model,
        "strategist",
        Insights,
        "Use web search to find 3 ad angles that competing fitness apps use. "
        "Describe each angle and cite the sources that support it.",
        search=True,
    )
