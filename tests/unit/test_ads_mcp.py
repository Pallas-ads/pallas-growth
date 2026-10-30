"""Reads through the Ads MCP, with the Graph API as the fallback, against fakes (no network)."""

import json
from datetime import date

import pytest

from pgt.ads.ads_mcp import AdsMcp, McpError, _sse_message
from pgt.ads.graph import Graph
from pgt.ads.meta_account import MetaAccount, WriteRefused

from .test_meta_account import ORIGIN, TOKEN, FakeGraph

SCHEMA = {
    "type": "object",
    "properties": {
        "ad_account_id": {"type": "string"},
        "level": {"type": "string", "enum": ["CAMPAIGN", "ADSET", "AD"]},
        "fields": {"type": "array", "items": {"type": "string"}},
        "date_preset": {"type": "string"},
        "time_range": {"type": "object"},
        "time_increment": {"type": "integer"},
    },
    "required": ["ad_account_id", "level"],
}


class FakeMcp:
    """Answers MCP JSON-RPC like the Ads MCP server, from the same data as the fake Graph."""

    def __init__(self, graph: FakeGraph, schema=SCHEMA, drop: tuple[str, ...] = (),
                 text_rows: bool = False):
        self.graph = graph
        self.schema = schema
        self.drop = drop  # fields this server leaves out of its rows
        self.text_rows = text_rows  # rows as a JSON string, spend as amount_spent {"value": ..}
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.fail_next = False

    def __call__(self, url, headers, body):
        assert url == "https://mcp.facebook.com/ads"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        self.headers.append(headers)
        method = body["method"]
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {}}, {"mcp-session-id": "s1"}
        assert headers.get("Mcp-Session-Id") == "s1"
        if method == "notifications/initialized":
            return None, {}
        if method == "tools/list":
            tools = [{"name": "ads_get_ad_accounts", "inputSchema": {}},
                     {"name": "ads_get_ad_entities", "inputSchema": self.schema}]
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": tools}}, {}
        args = body["params"]["arguments"]
        self.calls.append(args)
        if self.fail_next:
            self.fail_next = False
            reply = {"isError": True, "content": [{"type": "text", "text": "Rate limited"}]}
            return {"jsonrpc": "2.0", "id": body["id"], "result": reply}, {}
        rows = self._rows(args)
        rows = [{k: _without(v, self.drop) for k, v in r.items() if k not in self.drop}
                for r in rows]
        content = {"data": rows}
        if self.text_rows:
            rows = [{("amount_spent" if k == "spend" else k):
                     ({"value": v, "unit": "USD"} if k == "spend" else v) for k, v in r.items()}
                    for r in rows]
            content = {"ad_entities": json.dumps(rows)}
        reply = {"content": [{"type": "text", "text": "ignored"}], "structuredContent": content}
        return {"jsonrpc": "2.0", "id": body["id"], "result": reply}, {}

    def _rows(self, args):
        if args["level"] == "ADSET":
            # Numbers as numbers and the campaign as an id: shapes the Graph API does not use.
            return [{**a, "id": int(a["id"])} for a in self.graph.adsets]
        if args["level"] == "AD" and args.get("date_preset") == "maximum":
            return [{"id": "a1", "spend": 210.5, "actions": {"mobile_app_install": 30}}]
        if args["level"] == "AD" and "time_range" in args:
            assert args["time_increment"] == (1 if self.schema is SCHEMA else "1")
            return [{"date": "2026-10-23", "ad_id": "a1", "ad_set_id": "111",
                     "metrics": {"spend": 40, "impressions": 2000, "reach": 1000, "clicks": 50,
                                 "actions": [{"action_type": "mobile_app_install",
                                              "value": "4"}]}},
                    {"date": "2026-10-24", "ad_id": "a1", "ad_set_id": "111",
                     "metrics": {"spend": 45, "impressions": 3000, "reach": 1000, "clicks": 60,
                                 "actions": []}}]
        return list(self.graph.ads)


def _without(value, drop):
    return {k: v for k, v in value.items() if k not in drop} if isinstance(value, dict) else value


class CountingGraph(FakeGraph):
    def __init__(self):
        super().__init__()
        self.reads: list[str] = []

    def __call__(self, method, url, params):
        if method == "GET":
            self.reads.append(url.split("/v23.0/", 1)[1])
        return super().__call__(method, url, params)


def account(graph=None, mcp=None, **kw) -> MetaAccount:
    graph = graph or CountingGraph()
    return MetaAccount(Graph(TOKEN, graph), "42", ORIGIN, adset_ids=["111", "222"],
                       today=lambda: date(2026, 10, 30), currency="USD",
                       mcp=AdsMcp(TOKEN, mcp or FakeMcp(graph)), **kw)


def test_every_read_goes_through_the_mcp_and_matches_the_graph():
    graph = CountingGraph()
    fake = FakeMcp(graph)
    acc = account(graph, fake)
    state, rows = acc.state(), acc.insights()
    assert graph.reads == []  # nothing read from the Graph API
    assert acc.read_sources()["reads"] == {"ad_sets": "mcp", "ads": "mcp", "lifetime": "mcp",
                                           "daily": "mcp"}
    assert fake.calls[0]["ad_account_id"] == "42" and fake.calls[0]["level"] == "ADSET"

    plain = MetaAccount(Graph(TOKEN, FakeGraph()), "42", ORIGIN, adset_ids=["111", "222"],
                        today=lambda: date(2026, 10, 30))
    assert state == plain.state()
    assert rows == plain.insights()
    assert plain.read_sources()["mcp"] is False


def test_rows_sent_as_a_json_string_with_spend_as_amount_spent_are_read():
    graph = CountingGraph()
    schema = {**SCHEMA, "properties": {**SCHEMA["properties"],
                                       "time_increment": {"type": "string"},
                                       "time_range": {"type": "string"}}}
    fake = FakeMcp(graph, schema=schema, text_rows=True)
    acc = account(graph, fake)
    state, rows = acc.state(), acc.insights()
    assert graph.reads == []
    assert set(acc.read_sources()["reads"].values()) == {"mcp"}
    daily = next(c for c in fake.calls if "time_range" in c)
    assert json.loads(daily["time_range"]) == {"since": "2026-10-23", "until": "2026-10-29"}
    assert state.ad("a1").lifetime_spend == 210.5
    assert [r.spend for r in rows] == [40, 45]


def test_a_read_the_mcp_cannot_make_falls_back_to_the_graph():
    graph = CountingGraph()
    schema = {**SCHEMA, "properties": {k: v for k, v in SCHEMA["properties"].items()
                                       if k != "time_increment"}}
    fake = FakeMcp(graph, schema=schema)
    acc = account(graph, fake)
    acc.insights()
    acc.sync()
    acc.insights()
    sources = acc.read_sources()
    assert sources["reads"]["daily"] == "graph" and sources["reads"]["ad_sets"] == "mcp"
    assert "time_increment" in sources["notes"]["daily"]
    assert graph.reads == ["act_42/insights", "act_42/insights"]
    assert not any("time_range" in c for c in fake.calls)  # not asked again


def test_rows_missing_a_field_are_not_trusted():
    graph = CountingGraph()
    acc = account(graph, FakeMcp(graph, drop=("reach",)))
    rows = acc.insights()
    assert acc.read_sources()["reads"]["daily"] == "graph"
    assert "reach" in acc.read_sources()["notes"]["daily"]
    assert rows[1].frequency == 2.5  # read from the Graph API, reach included


def test_an_ad_set_without_its_budget_is_read_from_the_graph():
    graph = CountingGraph()
    fake = FakeMcp(graph)
    fake.graph = FakeGraph()
    fake.graph.adsets[1] = {**fake.graph.adsets[1], "campaign": "c2"}  # an id, not the budget
    acc = account(graph, fake)
    assert acc.state().campaign("222").daily_budget == 80.0
    assert acc.read_sources()["reads"]["ad_sets"] == "graph"


def test_a_failed_call_is_retried_on_the_next_read():
    graph = CountingGraph()
    fake = FakeMcp(graph)
    acc = account(graph, fake)
    fake.fail_next = True
    acc.state()
    assert acc.read_sources()["reads"]["ad_sets"] == "graph"
    assert "Rate limited" in acc.read_sources()["notes"]["ad_sets"]
    acc.sync()
    acc.state()
    assert acc.read_sources()["reads"]["ad_sets"] == "mcp"
    assert "ad_sets" not in acc.read_sources()["notes"]


def test_a_budget_write_checks_the_live_ad_set_on_the_graph():
    graph = CountingGraph()
    graph.adsets[1] = {**graph.adsets[1], "daily_budget": "8000"}  # what the MCP reports
    acc = account(graph)
    assert acc.state().campaign("222").daily_budget == 80.0
    del graph.adsets[1]["daily_budget"]  # the live ad set: its campaign owns the budget
    with pytest.raises(WriteRefused, match="Advantage campaign budget"):
        acc.writer().set_campaign_budget("222", 70.0)
    assert graph.posts == []


def test_mcp_errors_never_carry_the_token():
    def server(url, headers, body):
        if body["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {}}, {}
        if "id" not in body:
            return None, {}
        error = {"code": -32000, "message": f"bad token {TOKEN}"}
        return {"jsonrpc": "2.0", "id": body["id"], "error": error}, {}

    with pytest.raises(McpError) as caught:
        AdsMcp(TOKEN, server).tools()
    assert TOKEN not in str(caught.value) and "[token]" in str(caught.value)


def test_streamed_replies_are_read():
    text = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"tools":[]}}\n\n')
    assert _sse_message(text, 3) == {"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}
