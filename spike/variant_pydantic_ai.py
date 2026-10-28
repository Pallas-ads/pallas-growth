"""Pydantic AI per-role loops, using Meta's Responses API dialect."""

from time import perf_counter
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import AbstractCapability, WebSearch
from pydantic_ai.capabilities.abstract import WrapModelRequestHandler, WrapToolExecuteHandler
from pydantic_ai.messages import (
    ModelResponse,
    NativeToolCallPart,
    TextPart,
    ToolCallPart,
    WebCitationSource,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import RunContext, ToolDefinition

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
class _Harness(AbstractCapability):
    def __init__(self, role: str, log: EventLog) -> None:
        self.role = role
        self.log = log
        self.sources: list[Source] = []

    async def wrap_model_request(
        self,
        ctx: RunContext,
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        response = None
        started = perf_counter()
        try:
            response = await handler(request_context)
        finally:
            usage = (
                response.usage
                if response is not None and not response.usage.unmeasured_requests
                else None
            )
            self.log.add(
                "model.call",
                role=self.role,
                model=request_context.model.model_name,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                wall_time_s=perf_counter() - started,
            )
        for part in response.parts:
            if isinstance(part, NativeToolCallPart):
                self.log.add(
                    "tool.call", role=self.role, name=part.tool_name, args=part.args_as_dict()
                )
            if isinstance(part, TextPart):
                for citation in part.citations or []:
                    for source in citation.sources:
                        if isinstance(source, WebCitationSource):
                            self.sources.append(
                                Source(title=source.title or source.url, url=source.url)
                            )
        return response

    async def wrap_tool_execute(
        self,
        ctx: RunContext,
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        if error := policy_error(self.role, call.tool_name, self.log):
            return error
        self.log.add("tool.call", role=self.role, name=call.tool_name, args=call.args_as_dict())
        return await handler(args)


async def _run(client, log, model, role, output_type, instructions, tools=(), search=False):
    harness = _Harness(role, log)
    capabilities = [harness]
    # Hosted tools run remotely, so permission must be checked before exposing them.
    if search and policy_error(role, "web_search", log) is None:
        capabilities.append(WebSearch())
    responses = OpenAIResponsesModel(
        model,
        provider=OpenAIProvider(openai_client=client.with_options(max_retries=0)),
        profile=OpenAIModelProfile(supports_forced_tool_choice=False),
    )
    agent = Agent(
        responses,
        name=role,
        instructions=instructions,
        output_type=NativeOutput(output_type, strict=True),
        tools=tools,
        capabilities=capabilities,
        model_settings={"tool_choice": "auto", "openai_store": False},
    )
    agent.instrument = False
    result = (
        await agent.run("Complete your assigned task and return the typed deliverable.")
    ).output
    return fill_sources(result, harness.sources) if isinstance(result, Insights) else result


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
