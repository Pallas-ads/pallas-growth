"""The comparison bench, offline, on one seed: each harness part catches the mistake it is
meant to catch, and the single-agent baseline catches none of them."""

import asyncio

import pytest

from bench.run import ARMS, run_one
from bench.scenarios import BY_KEY

# Scenarios each arm is expected to fail. Everything else should pass.
EXPECTED_FAILURES = {
    "pgt": set(),
    "pgt-no-copy-checks": {"s3"},
    "pgt-no-reviewer": {"s2"},
    "pgt-no-hash": {"s6"},
    "pgt-role-writes": {"s5"},
    "single-agent": {"s2", "s3", "s5", "s6"},
}


def test_every_arm_has_an_expectation():
    assert set(EXPECTED_FAILURES) == set(ARMS)


@pytest.mark.parametrize("arm", list(EXPECTED_FAILURES))
def test_arm_fails_only_where_expected(arm, tmp_path):
    grades = [asyncio.run(run_one(arm, s, 7, tmp_path)) for s in BY_KEY.values()]
    failed = {g.scenario for g in grades if not g.passed}
    assert failed == EXPECTED_FAILURES[arm], {g.scenario: g.why for g in grades}


def test_full_team_blocks_and_logs_the_injection(tmp_path):
    g = asyncio.run(run_one("pgt", BY_KEY["s5"], 7, tmp_path))
    assert g.passed and g.completed
    assert g.blocked_tool_calls >= 1
    assert g.uncovered_writes == []


def test_edited_change_set_is_refused_not_applied(tmp_path):
    g = asyncio.run(run_one("pgt", BY_KEY["s6"], 7, tmp_path))
    assert g.stage == "refused" and g.uncovered_writes == []
