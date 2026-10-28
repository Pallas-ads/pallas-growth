# Framework spike: Pydantic AI vs OpenAI Agents SDK

Goal: pick the library for the per-role agent loop (PRD §8.6). The same small task is built twice, and the acceptance tests in `spike/tests/` run against both builds.

Everything here runs **offline** against a fake Meta Model API. Live runs against the real API happen later, only through `spike/run_live.py`, and only when Jieyi approves.

## Files to write

| File | Contents |
| --- | --- |
| `spike/__init__.py` | Empty |
| `spike/common.py` | Shared schemas, synthetic data, tools, event log, allowlist, and the fake Meta API (`MetaFake`). No agent-library imports here. |
| `spike/variant_pydantic_ai.py` | The task built with Pydantic AI |
| `spike/variant_openai_agents.py` | The task built with the OpenAI Agents SDK |
| `spike/run_live.py` | CLI for live runs (written now, **not run**) |

Do not edit `spike/tests/` or anything outside `spike/`, except `pyproject.toml` if a test-path setting is needed.

## `spike/common.py`

```python
class Finding(BaseModel):     claim: str; metric: str; value: float; evidence_ids: list[str]
class Findings(BaseModel):    findings: list[Finding]
class Source(BaseModel):      title: str; url: str
class MarketInsight(BaseModel): angle: str; summary: str; sources: list[Source]
class Insights(BaseModel):    insights: list[MarketInsight]
```

- `LEDGER: dict[str, dict]`: evidence rows keyed by evidence ID (`"ev-0001"` …). Synthetic fitness-app account: 1 campaign (`cmp_1`, $40/day), 3 ads (`ad_1` "Streak", `ad_2` "Progress", `ad_3` "Coach"). Plant fatigue on `ad_1`: frequency 4.1, CTR down 38% week over week. Each ad has one row for last week and one for this week.
- Tools (plain functions; each returns JSON-serializable data and includes `evidence_id` on every metric row):
  - `list_campaigns() -> list[dict]`
  - `get_ad_insights(ad_id: str) -> dict` (both weekly rows for that ad)
  - `update_budget(campaign_id: str, daily_budget: float) -> dict`: a write tool. It appends to the module-level list `WRITE_CALLS` when it runs. The Analyst is **given** this tool, but the policy layer must stop it from running.
  - `reset_state()`: clears `WRITE_CALLS`.
- `ALLOWLIST = {"analyst": {"list_campaigns", "get_ad_insights"}, "strategist": {"web_search"}}`
- `class EventLog`: `events: list[dict]`; `add(type: str, **payload)` appends `{"type": type, **payload}`; `of_type(type) -> list[dict]`.
  Event types the variants must emit:
  - `model.call`: once per HTTP request to the model, with `input_tokens` and `output_tokens` when available.
  - `tool.call`: once per executed tool, with `name` and `args`.
  - `tool.blocked`: once per tool call stopped by the allowlist, with `name` and `role`.
- `class MetaFake`: a scripted stand-in for `https://api.meta.ai/v1`, built on `httpx.MockTransport`.
  - `MetaFake(script: list[Step])`. A `Step` is one of:
    - `ToolCalls([("list_campaigns", {}), ("get_ad_insights", {"ad_id": "ad_1"})])`: the response contains one `function_call` output item per call.
    - `Final(obj: BaseModel | dict, citations: list[tuple[str, str]] = [], web_search: bool = False)`: the response contains an assistant `message` whose `output_text` is the JSON of `obj`. With `citations` (`(title, url)`), the text part has `url_citation` annotations. With `web_search=True`, a `web_search_call` output item comes first.
  - Serves `POST /v1/responses` with valid Responses API JSON (including `usage`). Any other path returns 404 and records a violation. After the script runs out, return 500 and record a violation.
  - `requests: list[dict]`: every request body received.
  - `violations: list[str]`: every Meta API rule a request breaks (see below).
  - `client() -> openai.AsyncOpenAI` with `base_url="https://api.meta.ai/v1"`, `api_key="test"`, and the mock transport.
- `check_meta_request(body: dict) -> list[str]` (used by `MetaFake`). Meta's rules:
  1. `tool_choice`, if present, must be `"auto"`.
  2. Hosted tools may only have type `"web_search"` (not `"web_search_preview"`). Function tools are fine.
  3. If `text.format.type == "json_schema"` and `strict` is true, then the schema must: have an object root; have no top-level `anyOf`/`oneOf`/`allOf`/`enum`/`not`; have no `allOf`/`oneOf` anywhere; have `additionalProperties: false` on every object; list every property in `required`; nest no deeper than 10; use no recursive `$ref`.
  4. `model` must be one of `muse-spark-1.3`, `muse-wonder-1.0`, or their `-contributor` variants.

## Each variant module

Expose exactly:

```python
async def run_analyst(client: AsyncOpenAI, log: EventLog, model: str = "muse-spark-1.3") -> Findings
async def run_strategist(client: AsyncOpenAI, log: EventLog, model: str = "muse-spark-1.3") -> Insights
GLUE_START = "# --- glue start ---"   # marker comments around the code needed to adapt the library to our harness
```

Requirements:
- Use the library's **Responses API** model class, built on the given `client`.
- **Typed output through JSON-schema output mode** (the response `text.format`), not tool-based output. Never send `tool_choice` other than `"auto"`.
- **Analyst:** instructions say to find the worst ad and cite evidence IDs. Tools: `list_campaigns`, `get_ad_insights`, `update_budget`.
- **Policy in code:** before any tool runs, check `ALLOWLIST[role]`. If the tool is not allowed, don't run it, emit `tool.blocked`, and return an error string to the model as the tool result. The run then continues normally.
- **Strategist:** the library's built-in web search tool. Map citations from the response into `MarketInsight.sources` if the model's JSON leaves them empty. Instructions: find 3 ad angles that competing fitness apps use.
- **Visibility:** emit `model.call` and `tool.call` events through the library's own hooks (hooks, processors, event streams or wrappers), not by re-parsing HTTP traffic.
- **No data leaves the machine.** For the Agents SDK, disable tracing export (`set_tracing_disabled(True)` or equivalent) so nothing goes to OpenAI. For Pydantic AI, don't enable Logfire.
- Mark the adapter code with `# --- glue start ---` and `# --- glue end ---` (it can be several blocks). The scorecard counts these lines.

## `spike/run_live.py` (write it, don't run it)

`uv run python -m spike.run_live --lib {pydantic_ai,openai_agents,both} [--contributor]`: load `MODEL_API_KEY` from `.env` with python-dotenv (never print it), run the Analyst with real tools and the Strategist with real web search, and print per-variant requests, input and output tokens, wall time, an estimated cost ($1.25 in / $4.25 out per 1M tokens, $2.50 per 1k searches), the events and the typed result. Save results to `spike/out/<lib>.json`.

## Done when

`uv run pytest spike/tests -q` passes, and `uv run ruff check spike` is clean. Report the command output and the glue line count for each variant.
