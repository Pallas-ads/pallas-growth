"""Runs one role as a Pydantic AI agent inside the harness: logged calls, cost caps, tool policy."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import WrapModelRequestHandler, WrapToolExecuteHandler
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.tools import RunContext, ToolDefinition

from pgt.harness.context import ContextPack
from pgt.harness.policy import CostMeter, check_tool
from pgt.harness.workspace import Workspace
from pgt.llm.meta import estimate_cost


@dataclass
class Scope:
    """Where a role runs: the workspace it logs to, its spend meter and the loop it belongs to."""

    ws: Workspace
    meter: CostMeter
    loop_id: str
    sim_day: int | None = None


class _Harness(AbstractCapability):
    def __init__(self, role: str, scope: Scope, pack: ContextPack) -> None:
        self.role, self.scope, self.pack = role, scope, pack

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
            usage = response.usage if response is not None else None
            model_name = request_context.model.model_name
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0
            cost = estimate_cost(model_name, input_tokens, output_tokens)
            self.scope.ws.events.append(
                "model.call",
                self.role,
                {
                    "role": self.role,
                    "model": model_name,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": round(cost, 6),
                    "wall_time_s": round(perf_counter() - started, 3),
                    "context_hash": self.pack.context_hash,
                    "fact_version": self.pack.fact_version,
                    "ok": response is not None,
                },
                refs=self.pack.inputs,
                sim_day=self.scope.sim_day,
                loop_id=self.scope.loop_id,
            )
            # Raises SpendCapExceeded, which stops the run before the next model call.
            if response is not None:
                self.scope.meter.record(self.role, self.scope.loop_id, cost, model=model_name)
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
        events = self.scope.ws.events
        if error := check_tool(self.role, call.tool_name, events, self.scope.loop_id):
            return error
        events.append(
            "tool.call",
            self.role,
            {"role": self.role, "tool": call.tool_name, "args": call.args_as_dict()},
            sim_day=self.scope.sim_day,
            loop_id=self.scope.loop_id,
        )
        return await handler(args)


async def run_role[T: BaseModel](
    role: str,
    output_type: type[T],
    pack: ContextPack,
    scope: Scope,
    model: Model,
    tools: list = (),
) -> T:
    """Run one role and return its typed deliverable (strict JSON schema output)."""
    agent = Agent(
        model,
        name=role,
        instructions=pack.instructions,
        output_type=NativeOutput(output_type, strict=True),
        tools=list(tools),
        capabilities=[_Harness(role, scope, pack)],
        model_settings={"tool_choice": "auto", "openai_store": False},
    )
    agent.instrument = False
    scope.ws.events.append(
        "context.assembled",
        role,
        {"role": role, "context_hash": pack.context_hash, "fact_version": pack.fact_version},
        refs=pack.inputs,
        sim_day=scope.sim_day,
        loop_id=scope.loop_id,
    )
    result = await agent.run(pack.prompt)
    return result.output
