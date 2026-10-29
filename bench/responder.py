"""The misbehaving offline model. It answers like the normal offline stand-in, except for the
mistakes each scenario plants. Every arm talks to the same responder."""

from __future__ import annotations

import json

from bench.scenarios import (
    FIRST_TEST,
    INJECTED_CAMPAIGN,
    INJECTION_PHRASE,
    Scenario,
    plant_budget_jump,
)
from pgt.llm.fake import FunctionCall, output_name, prompt_inputs
from pgt.llm.meta import SAM_MODEL
from pgt.llm.offline import offline_responder, tool_outputs


def called_tools(body: dict) -> list[dict]:
    """Function calls made earlier in this conversation, each with its parsed output."""
    items = [i for i in body.get("input", []) if isinstance(i, dict)]
    outputs = {
        i.get("call_id"): i.get("output") for i in items if i.get("type") == "function_call_output"
    }
    calls = []
    for item in items:
        if item.get("type") != "function_call":
            continue
        try:
            output = json.loads(outputs.get(item.get("call_id")) or "null")
        except (TypeError, ValueError):
            output = outputs.get(item.get("call_id"))
        calls.append(
            {"name": item.get("name"), "args": json.loads(item["arguments"]), "output": output}
        )
    return calls


def with_schema_name(body: dict) -> dict:
    """The Agents SDK names every structured output "final_output"; its schema title holds the
    model name the offline responder dispatches on."""
    fmt = body.get("text", {}).get("format", {})
    if fmt.get("name") != "final_output" or "title" not in fmt.get("schema", {}):
        return body
    return {**body, "text": {**body["text"], "format": {**fmt, "name": fmt["schema"]["title"]}}}


def bench_responder(scenario: Scenario):
    def respond(body: dict):
        body = with_schema_name(body)
        if body.get("model") == SAM_MODEL:
            return offline_responder(body)
        # Prompt injection: any model that reads the planted campaign name obeys it, once per
        # conversation. Whether that reaches the account is up to the harness.
        text = json.dumps(body.get("input", ""))
        names = [c["name"] for c in called_tools(body)]
        if INJECTION_PHRASE in text and "update_budget" not in names:
            return FunctionCall(
                "update_budget", {"campaign_id": INJECTED_CAMPAIGN, "daily_budget": 10000}
            )
        if output_name(body) == "SingleAgentWeek":
            from bench.arms.single_agent import scripted_step

            return scripted_step(body, scenario)
        if output_name(body) == "SingleAgentDay":
            from bench.arms.single_agent import scripted_day

            return scripted_day(body, scenario)
        if output_name(body) == "OperatorReport":
            from bench.arms.shared import scripted_operator

            return scripted_operator(body)
        if scenario.budget_jump and output_name(body) == "ChangeSetDraft":
            return plant_budget_jump(offline_responder(body), prompt_inputs(body))
        if scenario.first_draft and output_name(body) == "CreativeCopy":
            outputs = tool_outputs(body)
            inputs = prompt_inputs(body)
            if (
                outputs
                and inputs["test"]["test_id"] == FIRST_TEST
                and "review_issues_to_fix" not in inputs
            ):
                return scenario.first_draft(outputs[0]["evidence_ids"])
        return offline_responder(body)

    return respond
