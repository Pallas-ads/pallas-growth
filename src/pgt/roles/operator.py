"""Operator: applies approved, gate-allowed changes and creates approved ad drafts PAUSED.
It is code, not a model.

A write the account refuses (a real account's rules, or an error from Meta) is recorded on the
receipt and the Operator moves on; it never retries or works around a refusal."""

from __future__ import annotations

from pgt.ads.graph import GraphError
from pgt.ads.meta_account import WriteRefused
from pgt.harness import access
from pgt.harness.workspace import Workspace
from pgt.sandbox import SandboxAccount
from pgt.schemas import ChangeSet, CreativeSet, ExecutionReceipt

_FAILURES = (WriteRefused, GraphError, KeyError, ValueError, OSError)


def _creatives(ws: Workspace, record) -> dict[str, CreativeSet]:
    """The reviewed creative sets a change set was built from, by creative id."""
    out = {}
    for artifact_id in record.inputs:
        content = ws.artifacts.get(artifact_id).content
        if isinstance(content, dict) and "creative_id" in content and "ad_copy" in content:
            creative = CreativeSet.model_validate(content)
            out[creative.creative_id] = creative
    return out


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
    if not access.can_apply(ws):
        reason = (f"The team is set to {access.describe(access.level(ws))}. The Operator writes "
                  "only at L2 Apply after approval.")
        ws.events.append("changeset.refused", "operator", {"key": key, "reason": reason},
                         refs=[record.artifact_id], sim_day=record.sim_day)
        raise PermissionError(reason)
    change_set = ChangeSet.model_validate(record.content)
    approved = set(ws.approvals.approved_indexes(key))
    allowed = {g.change_index for g in change_set.gate if g.allowed}
    writer = account.writer()
    applied, skipped, errors = [], [], []
    for index, change in enumerate(change_set.changes):
        if index not in approved or index not in allowed:
            skipped.append(index)
            continue
        try:
            if change.kind == "set_budget":
                writer.set_campaign_budget(change.target_id, change.new_daily_budget)
            else:
                writer.set_ad_status(
                    change.target_id, "PAUSED" if change.kind == "pause_ad" else "ACTIVE"
                )
        except _FAILURES as exc:
            skipped.append(index)
            errors.append(f"Change {index + 1} ({change.kind} on {change.target_id}): "
                          f"{_message(exc)}")
            continue
        applied.append(index)
    # New ads from the weekly review are always created PAUSED; turning them on is a later,
    # separately approved change (and, on a real account, the owner's step in Ads Manager).
    creatives = _creatives(ws, record) if change_set.ad_drafts else {}
    created = []
    for draft in change_set.ad_drafts:
        try:
            created.append(writer.create_ad(draft.campaign_id, draft.name,
                                            creatives.get(draft.creative_id)))
        except _FAILURES as exc:
            errors.append(f"New ad {draft.name}: {_message(exc)}")
    receipt = ExecutionReceipt(
        change_set_id=change_set.change_set_id,
        content_hash=record.content_hash,
        applied=applied,
        skipped=skipped,
        day=account.day,
        created_ad_ids=created,
        errors=errors,
    )
    ws.events.append(
        "changeset.applied",
        "operator",
        receipt.model_dump(mode="json"),
        refs=[record.artifact_id],
        sim_day=account.day,
    )
    return receipt


def _message(exc: Exception) -> str:
    return f"{exc.args[0]} was not found" if isinstance(exc, KeyError) and exc.args else str(exc)
