"""Acceptance tests for the framework spike. Both variants must pass all of them offline."""

import asyncio
import importlib

import pytest

from spike import common
from spike.common import (
    LEDGER,
    EventLog,
    Final,
    Finding,
    Findings,
    Insights,
    MarketInsight,
    MetaFake,
    Source,
    ToolCalls,
    check_meta_request,
)

VARIANTS = ["spike.variant_pydantic_ai", "spike.variant_openai_agents"]


@pytest.fixture(params=VARIANTS)
def variant(request):
    common.reset_state()
    return importlib.import_module(request.param)


def _findings() -> Findings:
    return Findings(
        findings=[
            Finding(
                claim="Ad 'Streak' is fatigued: CTR fell 38% week over week at frequency 4.1",
                metric="ctr_change_wow",
                value=-0.38,
                evidence_ids=["ev-0001", "ev-0002"],
            )
        ]
    )


def _insights() -> Insights:
    return Insights(
        insights=[
            MarketInsight(angle=f"Angle {i}", summary="Seen in competitor ads.", sources=[])
            for i in range(3)
        ]
    )


# --- Fixture data sanity ---------------------------------------------------


def test_ledger_has_planted_fatigue():
    assert "ev-0001" in LEDGER and "ev-0002" in LEDGER
    streak = [row for row in LEDGER.values() if row.get("ad_id") == "ad_1"]
    assert len(streak) == 2
    assert any(abs(row.get("frequency", 0) - 4.1) < 1e-9 for row in streak)


def test_meta_request_checker():
    good = {"model": "muse-spark-1.3", "tool_choice": "auto", "input": "hi"}
    assert check_meta_request(good) == []
    assert check_meta_request({**good, "tool_choice": "required"})
    assert check_meta_request({**good, "tools": [{"type": "web_search_preview"}]})
    assert check_meta_request({**good, "model": "gpt-4o"})
    loose = {
        **good,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "x",
                "strict": True,
                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            }
        },
    }
    assert check_meta_request(loose), "missing required/additionalProperties must be flagged"


# --- Variant tests -----------------------------------------------------------


def test_analyst_typed_output_and_evidence(variant):
    fake = MetaFake(
        [
            ToolCalls([("list_campaigns", {})]),
            ToolCalls([("get_ad_insights", {"ad_id": "ad_1"})]),
            Final(_findings()),
        ]
    )
    log = EventLog()
    result = asyncio.run(variant.run_analyst(fake.client(), log))

    assert isinstance(result, Findings)
    for finding in result.findings:
        assert set(finding.evidence_ids) <= set(LEDGER)
    assert [e["name"] for e in log.of_type("tool.call")] == ["list_campaigns", "get_ad_insights"]
    assert len(log.of_type("model.call")) == len(fake.requests) == 3
    assert fake.violations == []


def test_blocked_tool_never_runs(variant):
    fake = MetaFake(
        [
            ToolCalls([("update_budget", {"campaign_id": "cmp_1", "daily_budget": 10000})]),
            Final(_findings()),
        ]
    )
    log = EventLog()
    result = asyncio.run(variant.run_analyst(fake.client(), log))

    assert isinstance(result, Findings)
    assert common.WRITE_CALLS == []
    blocked = log.of_type("tool.blocked")
    assert len(blocked) == 1 and blocked[0]["name"] == "update_budget"
    assert blocked[0]["role"] == "analyst"
    assert log.of_type("tool.call") == []
    assert fake.violations == []


def test_strategist_web_search_with_citations(variant):
    fake = MetaFake(
        [
            Final(
                _insights(),
                citations=[("Competitor ad library", "https://example.com/ads/1")],
                web_search=True,
            )
        ]
    )
    log = EventLog()
    result = asyncio.run(variant.run_strategist(fake.client(), log))

    assert isinstance(result, Insights) and len(result.insights) == 3
    urls = {s.url for i in result.insights for s in i.sources}
    assert "https://example.com/ads/1" in urls
    tools = fake.requests[0].get("tools", [])
    assert {"type": "web_search"} in [{"type": t["type"]} for t in tools]
    assert fake.violations == []


def test_roles_run_in_parallel(variant):
    analyst_fake = MetaFake([ToolCalls([("list_campaigns", {})]), Final(_findings())])
    strategist_fake = MetaFake([Final(_insights(), citations=[("Src", "https://example.com/a")])])
    log_a, log_s = EventLog(), EventLog()

    async def both():
        return await asyncio.gather(
            variant.run_analyst(analyst_fake.client(), log_a),
            variant.run_strategist(strategist_fake.client(), log_s),
        )

    findings, insights = asyncio.run(both())
    assert isinstance(findings, Findings) and isinstance(insights, Insights)
    assert analyst_fake.violations == [] and strategist_fake.violations == []


def test_requests_follow_meta_rules(variant):
    fake = MetaFake(
        [ToolCalls([("list_campaigns", {})]), Final(_findings())]
    )
    asyncio.run(variant.run_analyst(fake.client(), EventLog()))
    for body in fake.requests:
        assert body.get("tool_choice") in (None, "auto")
        fmt = body.get("text", {}).get("format", {})
        assert fmt.get("type") == "json_schema", "typed output must use JSON-schema output mode"
    assert fake.violations == []


def test_glue_markers_present(variant):
    source = open(variant.__file__).read()
    assert "# --- glue start ---" in source and "# --- glue end ---" in source


def test_source_model_shape():
    assert Source(title="t", url="https://x").url == "https://x"
