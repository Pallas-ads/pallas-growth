# Task B: Harness storage, policy gate and checks

Build the deterministic parts of the PGT harness (PRD §8.3 components 1, 2, 3, 7 and 8): the event log, artifact store, shared facts, evidence ledger, policy gate with house rules, approvals and number checks. No model calls, no network.

Read first: `AGENTS.md`, PRD §4.3 and §8.3 in `docs/PRD.md`, and everything in `src/pgt/schemas/`. Use those schemas as they are; do not edit them.

## Files you may write

`src/pgt/harness/`: `__init__.py`, `workspace.py`, `events.py`, `hashing.py`, `artifacts.py`, `facts.py`, `ledger.py`, `policy.py`, `approvals.py`, `verify.py`.

Do not touch any other file. The tests are `tests/unit/test_harness.py`; do not edit them. Another agent is writing `src/pgt/sandbox/` at the same time.

## `workspace.py`

```python
class Workspace:
    def __init__(self, path: str | Path = ":memory:") -> None
    events: EventLog; artifacts: ArtifactStore; facts: FactStore; ledger: EvidenceLedger; approvals: Approvals
    def close(self) -> None
```

All stores share one SQLite connection (`check_same_thread=False`) and write their events to the same `EventLog`. Reopening a file-backed workspace restores everything (the stores read from SQLite, not from memory-only state).

## `events.py`

```python
class Event(BaseModel): id: int; ts: str; sim_day: int | None; loop_id: str | None; actor: str; type: str; payload: dict; refs: list[str]
class EventLog:
    def append(self, type: str, actor: str, payload: dict | None = None, refs: list[str] | None = None,
               sim_day: int | None = None, loop_id: str | None = None) -> Event
    def list(self, type: str | None = None, loop_id: str | None = None, since_id: int = 0) -> list[Event]
```

- Keep the connection on `EventLog._conn` (the tests use it).
- Append-only: the `events` table has SQLite triggers that abort any UPDATE or DELETE. `ts` is UTC ISO 8601. IDs increase from 1.
- `list(type=...)` matches exactly, or as a prefix when the argument ends with `.` (`"tool."` matches `tool.call` and `tool.blocked`). Results are in id order.

## `hashing.py`

`content_hash(obj: BaseModel | dict, exclude: set[str] | None = None) -> str`: SHA-256 hex of canonical JSON (`model_dump(mode="json")` for models, `sort_keys=True`, `separators=(",", ":")`), with top-level keys in `exclude` left out.

## `artifacts.py`

A deliverable lives under a **key** (for example `"day3/change_set"`). Each publish under a key creates a new version.

```python
Status = Literal["draft", "in_review", "approved", "stale", "superseded"]
class ArtifactRecord(BaseModel):
    artifact_id: str            # f"{key}@v{version}"
    key: str; kind: str; version: int; author: str; status: Status
    content: dict; content_hash: str
    inputs: list[str]           # artifact_ids this was built from
    fact_version: int | None
    fact_keys: list[str]        # top-level SharedFacts fields it depends on
    evidence_ids: list[str]     # collected from the content: every "evidence_ids" list, anywhere in it
    sim_day: int | None

class ArtifactStore:
    def publish(self, key: str, content: BaseModel, author: str, inputs: list[str] = (),
                fact_version: int | None = None, fact_keys: list[str] = (),
                sim_day: int | None = None, status: Status = "draft") -> ArtifactRecord
    def get(self, artifact_id: str) -> ArtifactRecord | None
    def latest(self, key: str) -> ArtifactRecord | None
    def list(self, kind: str | None = None, status: Status | None = None) -> list[ArtifactRecord]
    def set_status(self, artifact_id: str, status: Status, actor: str) -> ArtifactRecord
    def mark_stale_for_facts(self, changed: list[str], new_version: int) -> list[str]
```

- `kind` is the content's class name. `content_hash` = `content_hash(content)`.
- Publishing a new version marks the previous version `superseded`, then marks stale every artifact that is not already `superseded` or `stale` and lists an older version of that key in its `inputs`, transitively. Emit `artifact.published` (refs: the new id) and one `artifact.stale` per stale artifact, with the reason in the payload.
- `mark_stale_for_facts`: every artifact that is not `superseded` or `stale`, whose `fact_keys` intersect `changed`, becomes stale, and so does everything downstream of it through `inputs`, transitively. Returns the stale ids. Artifacts with no overlapping `fact_keys` and no stale inputs are untouched. This is how "the budget changed, re-run only what depends on it" works (PRD §8.3 component 5).
- `set_status` emits `artifact.status`.

## `facts.py`

```python
class FactStore:
    version: int                                  # 0 before the first set
    def current(self) -> SharedFacts              # raises LookupError before the first set
    def get(self, version: int) -> SharedFacts
    def set(self, facts: SharedFacts, actor: str) -> int
```

- Only `actor in {"user", "growth_lead"}` may set facts; anyone else raises `PermissionError` and emits `fact.rejected`.
- `set` stores a new version, emits `fact.changed` with `payload = {"version": n, "changed": [top-level field names that differ]}`, then calls `artifacts.mark_stale_for_facts(changed, n)`. Setting identical facts creates no version and returns the current version.

## `ledger.py`

```python
class EvidenceLedger:
    def add(self, evidence: Evidence) -> None     # same id + same content is a no-op; same id + different content raises ValueError
    def add_rows(self, rows: list[InsightRow]) -> list[str]   # kind "insight"; values = every numeric field except day; None values left out
    def get(self, evidence_id: str) -> Evidence | None
    def has(self, evidence_id: str) -> bool
```

## `policy.py`

### Tool allowlist

```python
ALLOWLIST: dict[str, set[str]] = {
    "analyst": {"get_account_state", "get_insights", "get_evidence"},
    "media_buyer": {"get_account_state", "get_insights", "get_evidence"},
    "reviewer": {"get_evidence"},
    "strategist": {"web_search"},
    "growth_lead": {"get_account_state", "get_evidence"},
    "producer": set(),
}
def check_tool(role: str, tool: str, events: EventLog, loop_id: str | None = None) -> str | None
```

Returns None when allowed. Otherwise emits `tool.blocked` (payload: role, tool) and returns an error string for the model. Unknown roles have no tools.

### House rules

Each rule is a small class with a `name` and `check(change, state, facts) -> RuleResult`. `RULES` lists them in order. Adding a rule later means adding one class to `RULES`.

| `name` | Applies to | Fails when | Reason text must mention |
| --- | --- | --- | --- |
| `target_exists` | all | `set_budget` target is not a campaign in `state`; `pause_ad`/`activate_ad` target is not an ad; `set_budget` without a positive `new_daily_budget` | the target id |
| `min_days_between_budget_changes` | `set_budget` | `last_budget_change_day` is not None and `state.day - last_budget_change_day < min_days` | days since the last change and the minimum |
| `max_budget_change` | `set_budget` | `abs(new / old - 1) > max_budget_change_pct + 1e-9` | the change and the limit as whole percents, e.g. "+50%" and "20%" |
| `learning_phase_protection` | `set_budget` | `protect_learning_phase` and the campaign `in_learning` | "learning" |
| `min_spend_before_pause` | `pause_ad` | `ad.lifetime_spend < min_spend_multiple_before_pause × target_cost_per_result` | the spend and the threshold in dollars |
| `daily_budget_cap` | `set_budget` | the sum of ACTIVE campaign budgets, with this change applied, is above `daily_budget_cap` | the new total and the cap |

A rule that doesn't apply to a change kind is left out of that change's results. If `target_exists` fails, the other rules are skipped for that change.

```python
def gate_change_set(changes: list[Change], state: AccountState, facts: SharedFacts) -> list[GateResult]
```

Checks the changes in order against a **projected** copy of `state`. After a change is allowed, apply it to the projection (`set_budget` updates `daily_budget` and sets `last_budget_change_day = state.day`; `pause_ad`/`activate_ad` update the ad status), so later changes see its effect. A blocked change is not applied. Never mutate the `state` passed in.

### Model spend caps

```python
class SpendCapExceeded(Exception): ...
class CostMeter:
    def __init__(self, events: EventLog, per_call: float = 0.10, per_role: float = 0.50, per_loop: float = 2.00)
    def record(self, role: str, loop_id: str, cost: float, **details) -> None
    def spent(self, role: str | None = None, loop_id: str | None = None) -> float
```

`record` always emits `model.cost` (payload: role, cost, details). Then, if the call cost is above `per_call`, the role's total within the loop is above `per_role`, or the loop total is above `per_loop`, it emits `budget.exceeded` (payload says which cap) and raises `SpendCapExceeded`.

## `approvals.py`

```python
class Approvals:
    def grant(self, key: str, record: ArtifactRecord, actor: str = "user",
              approved: list[int] | None = None, override_rules: list[str] = (),
              confirm_override: bool = False) -> Event
    def is_valid(self, key: str) -> bool
    def approved_indexes(self, key: str) -> list[int]
```

- An approval binds to `(key, record.content_hash)`. `is_valid(key)` is True only when the latest version under `key` has the approved hash. When a valid approval stops matching, emit `approval.voided` once.
- `approved` lists which change indexes the user approved (None means all). `approved_indexes` returns them, or `[]` when not valid.
- **Override:** for `ChangeSet` content, if any approved change has a failed rule in its `gate`, every failed rule name must be listed in `override_rules` **and** `confirm_override` must be True; otherwise raise `PermissionError`. Overrides are recorded in the `approval.granted` payload.
- Only `actor == "user"` can grant; anyone else raises `PermissionError`.

## `verify.py`

```python
def number_matches(value: float, candidates: list[float]) -> bool
def check_findings(findings: list[Finding], ledger: EvidenceLedger) -> list[str]
def check_changes(changes: list[Change], ledger: EvidenceLedger) -> list[str]
```

- `number_matches`: True if for some candidate `x`, `math.isclose(value, x, rel_tol=0.015, abs_tol=1e-6)`, or the percent form `math.isclose(value, 100 * x, rel_tol=0.015, abs_tol=0.051)`.
- `check_findings` returns one plain-English issue per problem: a finding with no evidence IDs; an unknown evidence ID; a value that matches no number in the cited evidence.
- `check_changes`: each change must cite at least one evidence ID, and each must exist in the ledger.

## Done when

```bash
uv run pytest tests/unit/test_harness.py -q
uv run ruff check src/pgt/harness
```

Both pass. Report the exact output and the line count of each file.
