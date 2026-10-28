"""An offline stand-in for the Meta Model API, used by tests and offline demo runs.

It serves the Responses API over an in-memory transport, so requests go through the same
client code as live runs, and it checks every request against Meta's rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel


@dataclass
class FunctionCall:
    """Return this from a responder to make the fake model call a tool."""

    name: str
    arguments: dict


Responder = Callable[[dict], "BaseModel | dict | FunctionCall"]

META_MODELS = {"muse-spark-1.3", "muse-wonder-1.0"}


def check_meta_request(body: dict) -> list[str]:
    """Return the Meta Responses API rules this request breaks (the subset PGT relies on)."""
    violations: list[str] = []
    if body.get("tool_choice", "auto") != "auto":
        violations.append("tool_choice must be auto")
    for tool in body.get("tools", []):
        if tool.get("type") not in {"function", "web_search"}:
            violations.append(f"Unsupported hosted tool: {tool.get('type')}")
    if body.get("model", "").removesuffix("-contributor") not in META_MODELS:
        violations.append("Unsupported Meta model")
    fmt = body.get("text", {}).get("format", {})
    if fmt.get("type") == "json_schema" and fmt.get("strict"):
        schema = fmt.get("schema", {})
        if schema.get("type") != "object":
            violations.append("Strict schema root must be an object")
        _check_schema(schema, 1, violations)
        for definition in schema.get("$defs", {}).values():
            _check_schema(definition, 1, violations)
    return list(dict.fromkeys(violations))


def _check_schema(node: Any, depth: int, violations: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if depth > 10:
        violations.append("Strict schema nesting exceeds 10")
        return
    for keyword in ("allOf", "oneOf"):
        if keyword in node:
            violations.append(f"Strict schema cannot use {keyword}")
    if node.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            violations.append("Every object needs additionalProperties: false")
        if not set(node.get("properties", {})) <= set(node.get("required", [])):
            violations.append("Every object property must be required")
        for child in node.get("properties", {}).values():
            _check_schema(child, depth + 1, violations)
    for child in node.get("anyOf", []):
        _check_schema(child, depth, violations)
    _check_schema(node.get("items"), depth + 1, violations)


def prompt_inputs(body: dict) -> dict:
    """Pull the JSON inputs block that the context assembler puts in every prompt."""
    raw = body.get("input", "")
    text = raw if isinstance(raw, str) else _input_text(body)
    start = text.find("```json\n")
    end = text.find("\n```", start + 8)
    return json.loads(text[start + 8 : end]) if start >= 0 and end > start else {}


def _input_text(body: dict) -> str:
    parts = []
    for item in body.get("input", []):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [c.get("text", "") for c in content if isinstance(c, dict)]
    return "\n".join(parts)


def output_name(body: dict) -> str:
    return body.get("text", {}).get("format", {}).get("name", "")


class MetaFake:
    """Serves POST /v1/responses. `responder(body)` returns the typed output for each request."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[dict] = []
        self.violations: list[str] = []
        self._count = 0

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url="https://api.meta.ai/v1",
            api_key="offline",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._respond)),
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if request.method != "POST" or request.url.path != "/v1/responses":
            self.violations.append(f"Unsupported endpoint: {request.url.path}")
            return httpx.Response(404, json={"error": {"message": "Unknown endpoint"}})
        self.violations.extend(check_meta_request(body))
        self._count += 1
        obj = self.responder(body)
        if isinstance(obj, FunctionCall):
            text = json.dumps(obj.arguments)
            item = {
                "type": "function_call",
                "id": f"fc_{self._count}",
                "call_id": f"call_{self._count}",
                "name": obj.name,
                "arguments": text,
                "status": "completed",
            }
        else:
            text = json.dumps(obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj)
            item = {
                "type": "message",
                "id": f"msg_{self._count}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        # Rough token counts so offline runs still show plausible costs.
        input_tokens = len(request.content) // 4
        output_tokens = len(text) // 4
        return httpx.Response(
            200,
            json={
                "id": f"resp_{self._count}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": body.get("model"),
                "output": [item],
                "parallel_tool_calls": True,
                "tool_choice": body.get("tool_choice", "auto"),
                "tools": body.get("tools", []),
                "text": body.get("text", {"format": {"type": "text"}}),
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
