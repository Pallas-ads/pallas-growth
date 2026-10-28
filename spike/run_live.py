"""Explicit live entrypoint. Do not run without approval for paid Meta calls."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from agents.exceptions import AgentsException
from openai import APIError, AsyncOpenAI
from pydantic_ai.exceptions import AgentRunError

from spike.common import EventLog


async def _run_variant(library: str, client: AsyncOpenAI, model: str) -> dict:
    variant = importlib.import_module(f"spike.variant_{library}")
    log = EventLog()
    started = perf_counter()
    findings, insights = await asyncio.gather(
        variant.run_analyst(client, log, model),
        variant.run_strategist(client, log, model),
    )
    elapsed = perf_counter() - started
    calls = log.of_type("model.call")
    input_tokens = sum(call.get("input_tokens") or 0 for call in calls)
    output_tokens = sum(call.get("output_tokens") or 0 for call in calls)
    searches = sum(
        call["name"] == "web_search" and call["args"].get("type") == "search"
        for call in log.of_type("tool.call")
    )
    costs = {
        "input_tokens": Decimal(input_tokens) * Decimal("1.25") / 1_000_000,
        "output_tokens": Decimal(output_tokens) * Decimal("4.25") / 1_000_000,
        "web_searches": Decimal(searches) * Decimal("2.50") / 1_000,
    }
    return {
        "library": library,
        "model": model,
        "requests": len(calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "unmeasured_requests": sum(
            call.get("input_tokens") is None or call.get("output_tokens") is None for call in calls
        ),
        "web_searches": searches,
        "wall_time_s": round(elapsed, 6),
        "estimated_cost_usd": float(sum(costs.values())),
        "cost_breakdown_usd": {name: float(cost) for name, cost in costs.items()},
        "events": log.events,
        "result": {
            "analyst": findings.model_dump(mode="json"),
            "strategist": insights.model_dump(mode="json"),
        },
    }


async def _main(libraries: list[str], contributor: bool, api_key: str) -> None:
    model = "muse-spark-1.3" + ("-contributor" if contributor else "")
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    for library in libraries:
        async with AsyncOpenAI(
            base_url="https://api.meta.ai/v1", api_key=api_key, max_retries=0, timeout=120.0
        ) as client:
            report = await _run_variant(library, client, model)
        serialized = json.dumps(report, indent=2)
        (out / f"{library}.json").write_text(serialized + "\n", encoding="utf-8")
        print(serialized)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib", choices=("pydantic_ai", "openai_agents", "both"), required=True)
    parser.add_argument("--contributor", action="store_true")
    args = parser.parse_args()

    # Credential loading is confined to an explicit invocation of this live CLI.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    api_key = os.environ.get("MODEL_API_KEY")
    if not api_key:
        parser.error("MODEL_API_KEY is required for an approved live run")
    libraries = ["pydantic_ai", "openai_agents"] if args.lib == "both" else [args.lib]
    try:
        asyncio.run(_main(libraries, args.contributor, api_key))
    except (APIError, AgentsException, AgentRunError, OSError) as error:
        # Avoid printing provider exceptions that could contain credentials.
        raise SystemExit(f"Live run failed ({type(error).__name__}).") from None


if __name__ == "__main__":
    main()
