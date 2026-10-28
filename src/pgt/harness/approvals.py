"""User approvals bound to persisted artifact content hashes."""

from pgt.harness.artifacts import ArtifactRecord, ArtifactStore
from pgt.harness.events import Event, EventLog
from pgt.schemas import ChangeSet


class Approvals:
    def __init__(self, events: EventLog, artifacts: ArtifactStore) -> None:
        self._events, self._artifacts = events, artifacts

    def grant(
        self, key: str, record: ArtifactRecord, actor: str = "user",
        approved: list[int] | None = None, override_rules: list[str] = (),
        confirm_override: bool = False,
    ) -> Event:
        if actor != "user":
            raise PermissionError("Only the user may grant approvals")
        with self._events._transaction():
            stored = self._artifacts.get(record.artifact_id)
            if (record.key != key or stored is None or stored.key != key
                    or stored.content_hash != record.content_hash or stored.content != record.content):
                raise ValueError("Approval must refer to the stored artifact under this key")
            record = stored
            changes = record.content.get("changes", [])
            indexes = list(range(len(changes))) if approved is None else list(approved)
            if any(type(i) is not int or i < 0 or i >= len(changes) for i in indexes):
                raise ValueError("Approved change index is out of range")
            indexes = list(dict.fromkeys(indexes))
            if record.kind == "ChangeSet":
                change_set = ChangeSet.model_validate(record.content)
                failed = {result.rule for gate in change_set.gate
                          if gate.change_index in indexes
                          for result in gate.results if not result.passed}
                if failed and (not confirm_override or not failed.issubset(override_rules)):
                    raise PermissionError(
                        "Failed rules require explicit overrides and a second confirmation: "
                        + ", ".join(sorted(failed))
                    )
            return self._events.append(
                "approval.granted", actor,
                {"key": key, "content_hash": record.content_hash, "approved": indexes,
                 "override_rules": list(override_rules), "confirm_override": confirm_override},
                refs=[record.artifact_id], sim_day=record.sim_day,
            )

    def _latest_grant(self, key: str) -> Event | None:
        grants = [event for event in self._events.list("approval.granted")
                  if event.payload["key"] == key]
        return grants[-1] if grants else None

    def is_valid(self, key: str) -> bool:
        with self._events._transaction():
            grant = self._latest_grant(key)
            if grant is None:
                return False
            if any(event.payload.get("approval_id") == grant.id for event in self._events.list(
                "approval.voided", since_id=grant.id
            )):
                return False
            latest = self._artifacts.latest(key)
            if latest is not None and latest.content_hash == grant.payload["content_hash"]:
                return True
            self._events.append(
                "approval.voided", "system",
                {"key": key, "approval_id": grant.id, "content_hash": grant.payload["content_hash"],
                 "reason": "Latest artifact no longer matches approved content"},
                refs=[latest.artifact_id] if latest else [],
            )
            return False

    def approved_indexes(self, key: str) -> list[int]:
        with self._events._transaction():
            if not self.is_valid(key):
                return []
            return list(self._latest_grant(key).payload["approved"])
