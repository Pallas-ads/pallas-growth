"""Read-only tools that roles may call. The harness checks the allowlist before each call."""

from __future__ import annotations

from pydantic_ai import Tool

from pgt.llm.runtime import Scope


def evidence_tool(scope: Scope) -> Tool:
    def get_evidence(evidence_id: str) -> dict:
        """Look up one evidence entry by ID: its numbers, inputs and note."""
        evidence = scope.ws.ledger.get(evidence_id)
        if evidence is None:
            return {"error": f"Unknown evidence ID {evidence_id}."}
        return evidence.model_dump(mode="json")

    return Tool(get_evidence, takes_ctx=False)
