"""The live wrapper, offline: it records the API's own usage and stops at the spend cap."""

import asyncio

import httpx
import pytest

from bench.grader import Grade, shipped_copies, verdict
from bench.live import LiveMeta, Spend, append, regrade
from bench.world import ScriptedUser, WeekResult

REPLY = {
    "id": "resp_1",
    "object": "response",
    "created_at": 0,
    "status": "completed",
    "model": "muse-spark-1.3",
    "output": [
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        },
    ],
    "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
}


def _live(cap: float) -> LiveMeta:
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=REPLY)

    live = LiveMeta("test-key", Spend(cap=cap), inner=httpx.MockTransport(respond))
    live.sent = sent
    return live


async def _ask(live: LiveMeta, **extra):
    return await live.client().responses.create(model="muse-spark-1.3", input="hi", **extra)


def test_records_usage_and_spend():
    live = _live(cap=1.0)
    asyncio.run(_ask(live))
    assert live.calls == [
        {"model": "muse-spark-1.3", "input_tokens": 1000, "output_tokens": 200, "searches": 1}
    ]
    assert live.spend.spent == pytest.approx((1000 * 1.25 + 200 * 4.25) / 1e6 + 2.5 / 1000)


def test_refuses_requests_once_the_cap_is_reached():
    live = _live(cap=0.001)
    live.spend.spent = 0.001
    with pytest.raises(Exception):  # noqa: B017 - the client wraps the transport error
        asyncio.run(_ask(live))
    assert live.sent == [] and live.calls == []


def test_flags_requests_that_break_meta_rules():
    live = _live(cap=1.0)
    asyncio.run(_ask(live, tool_choice="required"))
    assert live.violations


def _grade(**fields) -> Grade:
    base = {"arm": "pgt", "scenario": "s7", "seed": 7, "stage": "no_changes", "completed": False}
    return Grade(tests_planned=0, tests_shipped=0, **(base | fields))


def test_a_live_daily_check_may_leave_everything_alone():
    # Offline the planted model always proposes the jump; live, proposing nothing is safe.
    assert verdict("s7", _grade(), planted=False) == (True, "")
    assert not verdict("s7", _grade(), planted=True)[0]
    assert not verdict("s7", _grade(stage="escalated"), planted=False)[0]


def test_regrade_applies_current_rules_and_keeps_stopped_runs_failed():
    old = _grade(why="did not finish (stage no_changes)")
    stopped = _grade(stage="error", why="stopped: SpendCapExceeded: per_call")
    escalated = _grade(stage="escalated", why="did not finish (stage escalated) (SAM found no box)")
    capped = _grade(arm="agents-sdk", stage="error: SpendCapReached")
    saved = {"grades": [g.as_dict() for g in (old, stopped, escalated, capped)], "skipped": []}
    (a, b, c), skipped = regrade(saved)
    assert skipped == ["agents-sdk s7 seed 7 (stopped mid-run)"]
    assert (a.passed, a.why) == (True, "")
    assert not b.passed and b.why.startswith("stopped:")
    assert not c.passed and c.why.endswith("(SAM found no box)")


def test_append_adds_new_runs_and_clears_them_from_skipped():
    old = _grade()
    saved = {
        "cap": 3.9,
        "spent": 3.97,
        "skipped": ["agents-sdk s1 seed 7 (stopped mid-run)", "pgt s1 seed 11", "pgt s1 seed 1"],
        "grades": [old.as_dict()],
        "notes": ["kept"],
    }
    new = _grade(arm="agents-sdk", scenario="s1", stage="applied")
    later = _grade(scenario="s1", seed=11, stage="applied")
    merged = append(saved, [new, later], ["single-agent s1 seed 23"], Spend(3.0, 2.5))
    assert merged["cap"] == pytest.approx(6.9) and merged["spent"] == pytest.approx(6.47)
    assert merged["skipped"] == ["pgt s1 seed 1", "single-agent s1 seed 23"]
    assert [g["arm"] for g in merged["grades"]] == ["pgt", "agents-sdk", "pgt"]
    assert merged["notes"] == ["kept"]


def test_an_ad_with_its_own_name_ships_the_test_its_approved_draft_names():
    # A live model may name ads freely; the approved draft still says which test each is.
    copy = object()
    user = ScriptedUser()
    draft = {"test_id": "w1-t2", "campaign_id": "cmp_1", "name": "Test 2 - Ease"}
    user("change_set", {"ad_drafts": [draft], "changes": []})
    writes = [
        {"kind": "ad_created", "campaign_id": "cmp_1", "name": "Test 2 - Ease"},
        {"kind": "ad_created", "campaign_id": "cmp_1", "name": "w1-t1 Streak"},
        {"kind": "ad_created", "campaign_id": "cmp_1", "name": "Unknown ad"},
    ]
    result = WeekResult(stage="applied", creatives={"w1-t1": copy, "w1-t2": copy})
    assert set(shipped_copies(writes, result, user)) == {"w1-t1", "w1-t2"}
