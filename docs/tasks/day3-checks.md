# Task D: Code checks for the weekly review

Build the deterministic checks the Reviewer runs before any model looks at weekly-review work (PRD §4.1 role 7, §8.3 component 8): ad copy, creative images, the test plan, market research and answers between roles. No model calls, no network.

Read first: `AGENTS.md`, PRD §4.1, §7.2 and §8.3 in `docs/PRD.md`, `src/pgt/schemas/weekly.py`, and `src/pgt/harness/verify.py` (reuse `number_matches` from it; do not edit it).

## Files you may write

`src/pgt/harness/creative_checks.py`. Nothing else. The tests are `tests/unit/test_creative_checks.py`; do not edit them. Another agent is writing `src/pgt/creative/` at the same time.

## API

```python
COPY_LIMITS: dict[str, int] = {"headline": 40, "primary_text": 125, "description": 30}

def extract_numbers(text: str) -> list[float]
def allocate_budget(weights: list[int], total: float) -> list[float]
def check_copy(ad_copy: CreativeCopy, ledger: EvidenceLedger, fact_numbers: list[float], known_urls: set[str]) -> list[str]
def check_images(images: list[CreativeImage]) -> list[str]
def check_plan(draft: TestPlanDraft, *, reviewed_evidence_ids: set[str], insight_ids: set[str],
               active_campaign_ids: set[str], ad_ids: set[str], min_tests: int = 2, max_tests: int = 3) -> list[str]
def check_research(research: MarketResearch) -> list[str]
def check_answer(answer: Answer, ledger: EvidenceLedger) -> list[str]
```

Every `check_*` returns a list of plain-English issues, empty when the input passes. Issues must contain the phrases quoted below (the tests match on them).

## Rules

### `extract_numbers`

Numbers a reader would take as a factual claim, in order of appearance:

| Text | Result |
|---|---|
| `Join 10k+ people` | `[10000]` (`k` = thousand, `m`/`M` = million, only when not followed by another letter) |
| `1,200 workouts` | `[1200]` |
| `$2.40 per trial` | `[2.4]` |
| `38% more` | `[38]` (keep percents as written) |
| `10-minute workouts` | `[10]` |
| `3.5x faster, 24/7` | `[3.5, 24, 7]` |
| `Rated #1 app` | `[]` (a number right after `#` is a rank; the model Reviewer judges those) |
| `Your 1st streak` | `[]` (ordinals `st`, `nd`, `rd`, `th` are ignored) |

### `allocate_budget`

Split `total` dollars in proportion to `weights`, rounded to cents. The last item takes the rounding remainder so the parts add up to exactly `round(total, 2)`. Raise `ValueError` for an empty list or any weight below 1.

### `check_copy`

- Length: `headline`, `primary_text` and `description` within `COPY_LIMITS`. Issue: `"{field} is {n} characters; the limit is {limit}."`
- Claims: each claim needs at least one evidence ID or source URL (`"Claim {i} has no evidence IDs or source URLs."`). Evidence IDs must exist in the ledger (`"Claim {i} cites unknown evidence {id}."`). Source URLs must be in `known_urls`, the URLs the Strategist's research found (`"Claim {i} cites a source URL that research did not find: {url}."`).
- Numbers: every number in `headline`, `primary_text` and `description` must be supported. A number is supported when `number_matches` finds it among the absolute values of all evidence the copy's claims cite, or among `fact_numbers` (numbers from the shared facts). Issue: `"Number {value:g} in {field} matches no cited evidence or shared fact."`

### `check_images`

Exactly one image per ratio in `CREATIVE_SIZES`.

- `"Missing image for {ratio}."`, `"Duplicate image for {ratio}."`
- `width`/`height` must equal `CREATIVE_SIZES[ratio]`: `"Image {ratio} is {w}x{h}; expected {W}x{H}."`
- The file at `path` must exist (`"Image {ratio} file is missing."`), its SHA-256 must equal `sha256` (`"Image {ratio} file does not match its sha256."`), and its real pixel size must match too (same "is WxH; expected" message). Open it with Pillow.

### `check_plan`

Labels are `"Test {i}"` with the 0-based index.

- Count: `"The plan has {n} test(s); it needs {min_tests} to {max_tests}."`
- Names unique: `"Test names must be unique."`
- `budget_weight` 1 to 5: `"Test {i} budget_weight {w} is outside 1-5."`
- Evidence: at least one ID (`"Test {i} cites no evidence."`), and every ID in `reviewed_evidence_ids` (`"Test {i} cites evidence {id} that is not in this week's reviewed findings."`).
- Insights: the brief needs at least one (`"Test {i} has no insight IDs."`), each in `insight_ids` (`"Test {i} cites unknown insight {id}."`).
- `campaign_id` in `active_campaign_ids`: `"Test {i} uses campaign {id}, which is not active."`
- `replaces_ad_id` is `None` or in `ad_ids`: `"Test {i} replaces unknown ad {id}."`

### `check_research`

- At least one insight: `"Research has no insights."`
- Each insight needs non-empty `claim`, `angle` and `hook` (`"Insight {i} is missing {field}."`) and at least one source whose URL starts with `http://` or `https://` (`"Insight {i} has no http(s) source."`).

### `check_answer`

- At least one evidence ID: `"The answer cites no evidence."`
- Each must exist: `"The answer cites unknown evidence {id}."`
- Every number in `answer` (via `extract_numbers`) must match the absolute values of the cited evidence with `number_matches`: `"Number {value:g} in the answer matches no cited evidence."`

## Done when

```bash
uv run pytest tests/unit/test_creative_checks.py -q
uv run ruff check src/pgt/harness/creative_checks.py
```

Both pass. Report the exact output.
