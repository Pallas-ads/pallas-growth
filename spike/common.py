"""Shared harness and synthetic Responses API fixtures. No agent-library dependencies."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict


class _Deliverable(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Deliverable):
    claim: str
    metric: str
    value: float
    evidence_ids: list[str]


class Findings(_Deliverable):
    findings: list[Finding]


class Source(_Deliverable):
    title: str
    url: str


class MarketInsight(_Deliverable):
    angle: str
    summary: str
    sources: list[Source]


class Insights(_Deliverable):
    insights: list[MarketInsight]


LEDGER: dict[str, dict] = {}
for _index, (_name, _clicks, _frequencies) in enumerate(
    [
        ("Streak", (200, 124), (2.5, 4.1)),
        ("Progress", (150, 160), (2.2, 2.4)),
        ("Coach", (180, 185), (2.0, 2.1)),
    ]
):
    for _week, _period in enumerate(("last_week", "this_week")):
        _evidence_id = f"ev-{2 * _index + _week + 1:04d}"
        LEDGER[_evidence_id] = {
            "evidence_id": _evidence_id,
            "campaign_id": "cmp_1",
            "campaign_daily_budget": 40.0,
            "ad_id": f"ad_{_index + 1}",
            "ad_name": _name,
            "period": _period,
            "spend": 100.0 if _index == 0 else 90.0,
            "impressions": 10_000,
            "clicks": _clicks[_week],
            "ctr": _clicks[_week] / 10_000,
            "frequency": _frequencies[_week],
        }
        if _week:
            LEDGER[_evidence_id].update(
                ctr_change_wow=round(_clicks[1] / _clicks[0] - 1, 6),
                evidence_ids=[f"ev-{2 * _index + 1:04d}", _evidence_id],
            )


WRITE_CALLS: list[dict] = []
ALLOWLIST = {
    "analyst": {"list_campaigns", "get_ad_insights"},
    "strategist": {"web_search"},
}


def list_campaigns() -> list[dict]:
    """List the synthetic campaign, its daily budget and its ads."""
    return [
        {
            "campaign_id": "cmp_1",
            "name": "Synthetic fitness app",
            "daily_budget": 40.0,
            "evidence_id": "ev-0002",
            "ads": [
                {"ad_id": row["ad_id"], "name": row["ad_name"]}
                for row in LEDGER.values()
                if row["period"] == "this_week"
            ],
        }
    ]


def get_ad_insights(ad_id: str) -> dict:
    """Return both weekly evidence rows, including the computed CTR change."""
    rows = [deepcopy(row) for row in LEDGER.values() if row["ad_id"] == ad_id]
    if not rows:
        return {"error": f"Unknown synthetic ad: {ad_id}"}
    return {"ad_id": ad_id, "rows": rows}


def update_budget(campaign_id: str, daily_budget: float) -> dict:
    """Record a synthetic budget write. The Analyst must never reach this function."""
    call = {"campaign_id": campaign_id, "daily_budget": daily_budget}
    WRITE_CALLS.append(call)
    return {"status": "updated", **call}


def reset_state() -> None:
    WRITE_CALLS.clear()


class EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, type: str, **payload: Any) -> None:
        self.events.append({"type": type, **payload})

    def of_type(self, type: str) -> list[dict]:
        return [event for event in self.events if event["type"] == type]


def policy_error(role: str, name: str, log: EventLog) -> str | None:
    """Check permissions without invoking the tool or relying on model instructions."""
    if name not in ALLOWLIST.get(role, set()):
        log.add("tool.blocked", name=name, role=role)
        return f"Error: tool {name!r} is not allowed for role {role!r}."
    return None


def fill_sources(result: Insights, citations: list[Source]) -> Insights:
    """Retain model-authored sources and fill omissions from response citations."""
    sources = {source.url: source for source in citations}
    for insight in result.insights:
        if not insight.sources:
            insight.sources = [source.model_copy() for source in sources.values()]
    return result


def check_meta_request(body: dict) -> list[str]:
    """Validate the subset of Meta's Responses rules exercised by this spike."""
    violations: list[str] = []
    if "tool_choice" in body and body["tool_choice"] != "auto":
        violations.append("tool_choice must be auto")
    for tool in body.get("tools", []):
        if tool.get("type") not in {"function", "web_search"}:
            violations.append(f"Unsupported hosted tool: {tool.get('type')}")
    models = {"muse-spark-1.3", "muse-wonder-1.0"}
    models |= {f"{model}-contributor" for model in models}
    if body.get("model") not in models:
        violations.append("Unsupported Meta model")

    fmt = body.get("text", {}).get("format", {})
    if fmt.get("type") != "json_schema" or fmt.get("strict") is not True:
        return violations
    schema = fmt.get("schema", {})
    if not isinstance(schema, dict) or schema.get("type") != "object":
        violations.append("Strict schema root must be an object")
    if not isinstance(schema, dict):
        return violations
    for keyword in ("anyOf", "oneOf", "allOf", "enum", "not"):
        if keyword in schema:
            violations.append(f"Strict schema root cannot use {keyword}")

    def resolve(ref: str) -> dict | None:
        if ref == "#":
            return schema
        if not ref.startswith("#/"):
            return None
        node: Any = schema
        for component in ref[2:].split("/"):
            key = component.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node if isinstance(node, dict) else None

    def visit(node: Any, depth: int, refs: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        if depth > 10:
            violations.append("Strict schema nesting exceeds 10")
            return
        for keyword in ("allOf", "oneOf"):
            if keyword in node:
                violations.append(f"Strict schema cannot use {keyword}")
        node_type = node.get("type")
        if (
            node_type == "object"
            or (isinstance(node_type, list) and "object" in node_type)
            or "properties" in node
        ):
            properties = node.get("properties", {})
            if node.get("additionalProperties") is not False:
                violations.append("Every object needs additionalProperties: false")
            required = node.get("required", [])
            if not isinstance(required, list) or not set(properties).issubset(required):
                violations.append("Every object property must be required")
            for child in properties.values():
                visit(child, depth + 1, refs)
        if "$ref" in node:
            ref = node["$ref"]
            if ref in refs:
                violations.append(f"Recursive schema reference: {ref}")
            elif (target := resolve(ref)) is None:
                violations.append(f"Unresolvable schema reference: {ref}")
            else:
                visit(target, depth, (*refs, ref))
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(keyword, []):
                visit(child, depth + (keyword == "prefixItems"), refs)
        for keyword in ("items", "additionalProperties", "contains", "not", "if", "then", "else"):
            visit(node.get(keyword), depth + (keyword in {"items", "contains"}), refs)

    visit(schema, 1)
    for keyword in ("$defs", "definitions"):
        for name, definition in schema.get(keyword, {}).items():
            escaped = name.replace("~", "~0").replace("/", "~1")
            visit(definition, 1, (f"#/{keyword}/{escaped}",))
    return list(dict.fromkeys(violations))


@dataclass
class ToolCalls:
    calls: list[tuple[str, dict]]


@dataclass
class Final:
    obj: BaseModel | dict
    citations: list[tuple[str, str]] = field(default_factory=list)
    web_search: bool = False


Step = ToolCalls | Final


class MetaFake:
    """Scripted Meta endpoint. Its transport cannot make network requests."""

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.violations: list[str] = []
        self._position = 0
        self._transport = httpx.MockTransport(self._respond)

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url="https://api.meta.ai/v1",
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=self._transport),
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if request.method != "POST" or request.url.path != "/v1/responses":
            self.violations.append(f"Unsupported endpoint: {request.method} {request.url.path}")
            return httpx.Response(404, json={"error": {"message": "Unknown fake endpoint"}})
        self.violations.extend(check_meta_request(body))
        if self._position >= len(self.script):
            self.violations.append("MetaFake script exhausted")
            return httpx.Response(500, json={"error": {"message": "Fake script exhausted"}})
        step = self.script[self._position]
        self._position += 1
        number = self._position
        output: list[dict] = []
        if isinstance(step, ToolCalls):
            for index, (name, args) in enumerate(step.calls):
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{number}_{index}",
                        "call_id": f"call_{number}_{index}",
                        "name": name,
                        "arguments": json.dumps(args),
                        "status": "completed",
                    }
                )
        else:
            obj = step.obj.model_dump(mode="json") if isinstance(step.obj, BaseModel) else step.obj
            content = json.dumps(obj)
            if step.web_search:
                output.append(
                    {
                        "type": "web_search_call",
                        "id": f"ws_{number}",
                        "status": "completed",
                        "action": {"type": "search", "query": "fitness app advertising angles"},
                    }
                )
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{number}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": content,
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": title,
                                    "url": url,
                                    "start_index": 0,
                                    "end_index": len(content),
                                }
                                for title, url in step.citations
                            ],
                        }
                    ],
                }
            )
        return httpx.Response(
            200,
            json={
                "id": f"resp_{number}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": body.get("model", "muse-spark-1.3"),
                "output": output,
                "parallel_tool_calls": body.get("parallel_tool_calls", True),
                "tool_choice": body.get("tool_choice", "auto"),
                "tools": body.get("tools", []),
                "text": body.get("text", {"format": {"type": "text"}}),
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
