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
    "agents-sdk": {"s3"},
    "langgraph": {"s3", "s6"},
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


@pytest.mark.parametrize("arm", ["pgt", "agents-sdk"])
def test_edited_change_set_is_refused_not_applied(arm, tmp_path):
    g = asyncio.run(run_one(arm, BY_KEY["s6"], 7, tmp_path))
    assert g.stage == "refused" and g.uncovered_writes == []


@pytest.mark.parametrize("arm", ["agents-sdk", "langgraph"])
def test_framework_returns_the_injected_call_to_the_model(arm, tmp_path):
    # The role was never given update_budget: the framework answers with an error and goes on.
    g = asyncio.run(run_one(arm, BY_KEY["s5"], 7, tmp_path))
    assert g.passed and g.completed
    assert g.blocked_tool_calls >= 1


@pytest.mark.parametrize("arm", ["agents-sdk", "langgraph"])
def test_framework_reviewer_sends_the_claim_back(arm, tmp_path):
    g = asyncio.run(run_one(arm, BY_KEY["s2"], 7, tmp_path))
    assert g.passed and g.redos >= 1


def test_langgraph_applies_a_change_set_edited_after_approval(tmp_path):
    # update_state between interrupt and resume: the tool runs with the edited arguments and
    # the user is not asked again.
    g = asyncio.run(run_one("langgraph", BY_KEY["s6"], 7, tmp_path))
    assert g.stage == "applied"
    assert len(g.uncovered_writes) == 1


@pytest.mark.parametrize("module", ["agents_sdk", "langgraph_team", "shared"])
def test_framework_arms_use_the_same_team_rules_and_no_harness_code(module):
    import importlib
    from pathlib import Path

    from pgt.harness.context import TEAM_RULES

    arm = importlib.import_module(f"bench.arms.{module}")
    assert arm.TEAM_RULES == TEAM_RULES
    lines = Path(arm.__file__).read_text().splitlines()
    assert not [line for line in lines if line.startswith(("from pgt.harness", "import pgt."))]
