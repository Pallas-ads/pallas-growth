"""Versioned typed deliverables and transitive dependency invalidation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from pgt.harness.events import EventLog
from pgt.harness.hashing import content_hash

Status = Literal["draft", "in_review", "approved", "stale", "superseded"]


class ArtifactRecord(BaseModel):
    artifact_id: str
    key: str
    kind: str
    version: int
    author: str
    status: Status
    content: dict
    content_hash: str
    inputs: list[str]
    fact_version: int | None
    fact_keys: list[str]
    evidence_ids: list[str]
    sim_day: int | None


def _evidence_ids(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" and isinstance(child, list):
                found.extend(item for item in child if isinstance(item, str))
            found.extend(_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_evidence_ids(child))
    return list(dict.fromkeys(found))


class ArtifactStore:
    def __init__(self, events: EventLog) -> None:
        self._events = events
        self._conn = events._conn
        with events._transaction():
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, key TEXT NOT NULL,
                    version INTEGER NOT NULL, record TEXT NOT NULL,
                    UNIQUE(key, version)
                )
            """)

    def publish(
        self, key: str, content: BaseModel, author: str, inputs: list[str] = (),
        fact_version: int | None = None, fact_keys: list[str] = (),
        sim_day: int | None = None, status: Status = "draft",
    ) -> ArtifactRecord:
        with self._events._transaction():
            previous = self.latest(key)
            version = previous.version + 1 if previous else 1
            data = content.model_dump(mode="json")
            record = ArtifactRecord(
                artifact_id=f"{key}@v{version}", key=key, kind=type(content).__name__,
                version=version, author=author, status=status, content=data,
                content_hash=content_hash(content), inputs=list(inputs),
                fact_version=fact_version, fact_keys=list(fact_keys),
                evidence_ids=_evidence_ids(data), sim_day=sim_day,
            )
            if previous:
                self.set_status(previous.artifact_id, "superseded", author)
            self._conn.execute(
                "INSERT INTO artifacts (artifact_id, key, version, record) VALUES (?, ?, ?, ?)",
                (record.artifact_id, key, version, record.model_dump_json()),
            )
            self._events.append(
                "artifact.published", author, record.model_dump(mode="json"),
                refs=[record.artifact_id], sim_day=sim_day,
            )
            older = {r.artifact_id for r in self.list() if r.key == key and r.version < version}
            self._mark_stale([], older, {"reason": "input superseded", "key": key})
            return record

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT record FROM artifacts WHERE artifact_id = ?", (artifact_id,),
        ).fetchone()
        return ArtifactRecord.model_validate_json(row[0]) if row else None

    def latest(self, key: str) -> ArtifactRecord | None:
        row = self._conn.execute(
            "SELECT record FROM artifacts WHERE key = ? ORDER BY version DESC LIMIT 1", (key,),
        ).fetchone()
        return ArtifactRecord.model_validate_json(row[0]) if row else None

    def list(self, kind: str | None = None, status: Status | None = None) -> list[ArtifactRecord]:
        records = [ArtifactRecord.model_validate_json(row[0]) for row in self._conn.execute(
            "SELECT record FROM artifacts ORDER BY rowid"
        ).fetchall()]
        return [r for r in records if (kind is None or r.kind == kind)
                and (status is None or r.status == status)]

    def _save(self, record: ArtifactRecord) -> None:
        self._conn.execute(
            "UPDATE artifacts SET record = ? WHERE artifact_id = ?",
            (record.model_dump_json(), record.artifact_id),
        )

    def set_status(self, artifact_id: str, status: Status, actor: str) -> ArtifactRecord:
        with self._events._transaction():
            record = self.get(artifact_id)
            if record is None:
                raise LookupError(f"Unknown artifact {artifact_id}")
            record = ArtifactRecord.model_validate({**record.model_dump(), "status": status})
            self._save(record)
            self._events.append(
                "artifact.status", actor, {"artifact_id": artifact_id, "status": status},
                refs=[artifact_id], sim_day=record.sim_day,
            )
            return record

    def _mark_stale(self, direct: list[str], seeds: set[str], reason: dict) -> list[str]:
        records = self.list()
        affected, stale = set(direct) | seeds, []
        while True:
            downstream = {r.artifact_id for r in records if affected.intersection(r.inputs)}
            if downstream.issubset(affected):
                break
            affected.update(downstream)
        # Applied change sets are history: what reached the account stays as it was (PRD §4.3).
        applied = {e.refs[0] for e in self._events.list("changeset.applied") if e.refs}
        for record in records:
            if (record.status in {"superseded", "stale"} or record.artifact_id not in affected
                    or record.artifact_id in applied):
                continue
            was, record.status = record.status, "stale"
            self._save(record)
            self._events.append(
                "artifact.stale", "system",
                {**reason, "artifact_id": record.artifact_id, "was": was},
                refs=[record.artifact_id], sim_day=record.sim_day,
            )
            stale.append(record.artifact_id)
        return stale

    def keep(self, artifact_id: str, reason: str, actor: str = "growth_lead") -> ArtifactRecord:
        """Undo a stale mark after a re-run showed that nothing this artifact uses changed (the
        early cutoff a build system makes when a rebuilt input comes out the same)."""
        with self._events._transaction():
            record = self.get(artifact_id)
            if record is None or record.status != "stale":
                raise ValueError(f"{artifact_id} is not stale")
            marks = [e for e in self._events.list("artifact.stale") if artifact_id in e.refs]
            record.status = marks[-1].payload.get("was", "in_review") if marks else "in_review"
            self._save(record)
            self._events.append("artifact.kept", actor,
                                {"artifact_id": artifact_id, "key": record.key, "reason": reason},
                                refs=[artifact_id], sim_day=record.sim_day)
            return record

    def mark_stale_for_facts(self, changed: list[str], new_version: int) -> list[str]:
        with self._events._transaction():
            direct = [r.artifact_id for r in self.list() if r.status not in {"superseded", "stale"}
                      and set(changed).intersection(r.fact_keys)]
            return self._mark_stale(
                direct, set(), {"reason": "facts changed", "changed": changed,
                                "fact_version": new_version},
            )
