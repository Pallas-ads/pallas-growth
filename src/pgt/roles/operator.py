"""Operator: applies approved, gate-allowed changes and creates approved ad drafts PAUSED.
It is code, not a model."""

from __future__ import annotations

from pgt.harness.workspace import Workspace
from pgt.sandbox import SandboxAccount
from pgt.schemas import ChangeSet, ExecutionReceipt


def apply_change_set(ws: Workspace, account: SandboxAccount, key: str) -> ExecutionReceipt:
    record = ws.artifacts.latest(key)
    if record is None:
        raise LookupError(f"No change set under {key}.")
    if not ws.approvals.is_valid(key):
        ws.events.append(
            "changeset.refused",
            "operator",
            {"key": key, "reason": "no valid approval"},
            refs=[record.artifact_id],
            sim_day=record.sim_day,
        )
        raise PermissionError(f"{key} has no valid approval for its current content.")
    change_set = ChangeSet.model_validate(record.content)
    approved = set(ws.approvals.approved_indexes(key))
    allowed = {g.change_index for g in change_set.gate if g.allowed}
    writer = account.writer()
    applied, skipped = [], []
    for index, change in enumerate(change_set.changes):
        if index not in approved or index not in allowed:
            skipped.append(index)
            continue
        if change.kind == "set_budget":
            writer.set_campaign_budget(change.target_id, change.new_daily_budget)
        else:
            writer.set_ad_status(
                change.target_id, "PAUSED" if change.kind == "pause_ad" else "ACTIVE"
            )
        applied.append(index)
    # New ads from the weekly review are always created PAUSED; turning them on is a later,
    # separately approved change.
    created = [writer.create_ad(d.campaign_id, d.name) for d in change_set.ad_drafts]
    receipt = ExecutionReceipt(
        change_set_id=change_set.change_set_id,
        content_hash=record.content_hash,
        applied=applied,
        skipped=skipped,
        day=account.day,
        created_ad_ids=created,
    )
    ws.events.append(
        "changeset.applied",
        "operator",
        receipt.model_dump(mode="json"),
        refs=[record.artifact_id],
        sim_day=account.day,
    )
    return receipt
