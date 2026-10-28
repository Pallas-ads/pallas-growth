"""OFFLINE STAND-IN for the Muse models, for tests and free demo runs.

This is a small rule-based responder, not a model. It answers the same requests a live run sends,
through the same client, schemas, harness and checks, so everything except the model's judgment
is exercised offline. Live runs (`pgt simulate --live`) use Muse Spark instead.
"""

from __future__ import annotations

from pydantic import BaseModel

from pgt.llm.fake import MetaFake, output_name, prompt_inputs
from pgt.llm.meta import DEFAULT_MODEL, responses_model
from pgt.roles.metrics import expected_readout
from pgt.schemas import Change, ChangeSetDraft, Finding, Findings, TestReadout


def offline_responder(body: dict) -> BaseModel:
    inputs = prompt_inputs(body)
    name = output_name(body)
    props = set(body.get("text", {}).get("format", {}).get("schema", {}).get("properties", {}))
    if name == "TestReadout" or "recommendation" in props:
        return _readout(inputs)
    if name == "ChangeSetDraft" or "changes" in props:
        return _changes(inputs)
    return _findings(inputs)


def _findings(inputs: dict) -> Findings:
    return Findings(
        findings=[
            Finding(
                subject_id=s["subject_id"],
                claim=s["note"],
                metric=s["metric"],
                value=s["value"],
                evidence_ids=s["evidence_ids"],
                severity=s["severity"],
            )
            for s in inputs.get("signals", [])
        ]
    )


def _readout(inputs: dict) -> TestReadout:
    (evidence_id, values), *_ = inputs["evidence"].items()
    recommendation = expected_readout(values)
    cpr, target = values["cost_per_result"], values["target_cost_per_result"]
    return TestReadout(
        test_id=inputs["test_id"],
        recommendation=recommendation,
        rationale=f"Cost per result ${cpr:g} against a ${target:g} target "
        f"over {values['results']:g} results.",
        findings=[
            Finding(
                subject_id=inputs["test_id"],
                claim=f"The test's cost per result is ${cpr:g}.",
                metric="cost_per_result",
                value=cpr,
                evidence_ids=[evidence_id],
                severity="act",
            )
        ],
    )


def _changes(inputs: dict) -> ChangeSetDraft:
    account = inputs["account"]
    ads = {a["ad_id"]: a for a in account["ads"]}
    campaigns = {c["test_id"]: c for c in account["campaigns"] if c.get("test_id")}
    changes: list[Change] = []
    used: set[str] = set()
    for f in inputs.get("findings", []):
        if f["severity"] != "act":
            continue
        ad = ads.get(f["subject_id"])
        if ad and ad["status"] == "ACTIVE" and ad["ad_id"] not in used:
            fresh = next(
                (
                    a
                    for a in account["ads"]
                    if a["campaign_id"] == ad["campaign_id"]
                    and a["status"] == "PAUSED"
                    and a["lifetime_spend"] == 0
                    and a["ad_id"] not in used
                ),
                None,
            )
            if fresh:
                used.add(fresh["ad_id"])
                changes.append(
                    _change("activate_ad", fresh["ad_id"], f, f"Swap in {fresh['name']}.")
                )
            used.add(ad["ad_id"])
            changes.append(_change("pause_ad", ad["ad_id"], f, f"{ad['name']}: {f['claim']}"))
        campaign = campaigns.get(f["subject_id"])
        if campaign and f["metric"] == "cost_per_result":
            target = inputs.get("target_cost_per_result")
            if target is None or f["value"] <= 0.85 * target:
                new_budget = round(campaign["daily_budget"] * 1.2, 2)
                changes.append(
                    _change(
                        "set_budget",
                        campaign["campaign_id"],
                        f,
                        "Scale the winning test 20%.",
                        new_budget,
                    )
                )
    summary = "Act on today's findings." if changes else "No change today: nothing calls for one."
    return ChangeSetDraft(summary=summary, changes=changes)


def _change(kind, target_id, finding, reason, budget=None) -> Change:
    return Change(
        kind=kind,
        target_id=target_id,
        new_daily_budget=budget,
        reason=reason,
        evidence_ids=finding["evidence_ids"],
    )


def offline_model(model: str = DEFAULT_MODEL):
    """Returns (model, fake). The fake records every request and any Meta rule it breaks."""
    fake = MetaFake(offline_responder)
    return responses_model(fake.client(), model), fake
