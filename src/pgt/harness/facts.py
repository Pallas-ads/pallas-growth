"""Versioned shared facts with restricted authorship."""

from pgt.harness.artifacts import ArtifactStore
from pgt.harness.events import EventLog
from pgt.schemas import SharedFacts


class FactStore:
    def __init__(self, events: EventLog, artifacts: ArtifactStore) -> None:
        self._events, self._artifacts = events, artifacts
        self._conn = events._conn
        with events._transaction():
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS facts (version INTEGER PRIMARY KEY, content TEXT NOT NULL)"
            )

    @property
    def version(self) -> int:
        return self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM facts").fetchone()[0]

    def current(self) -> SharedFacts:
        return self.get(self.version)

    def get(self, version: int) -> SharedFacts:
        row = self._conn.execute(
            "SELECT content FROM facts WHERE version = ?", (version,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown fact version {version}")
        return SharedFacts.model_validate_json(row[0])

    def set(self, facts: SharedFacts, actor: str) -> int:
        if actor not in {"user", "growth_lead"}:
            self._events.append("fact.rejected", actor, {"actor": actor})
            raise PermissionError(f"{actor} may not set shared facts")
        with self._events._transaction():
            version = self.version
            previous = self.current().model_dump(mode="json") if version else {}
            data = facts.model_dump(mode="json")
            changed = [key for key, value in data.items() if key not in previous
                       or previous[key] != value]
            if not changed:
                return version
            version += 1
            self._conn.execute(
                "INSERT INTO facts (version, content) VALUES (?, ?)",
                (version, facts.model_dump_json()),
            )
            self._events.append("fact.changed", actor, {"version": version, "changed": changed})
            self._artifacts.mark_stale_for_facts(changed, version)
            return version
