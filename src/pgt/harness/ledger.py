"""Persistent evidence identities and numeric values."""

from pgt.harness.events import EventLog
from pgt.schemas import Evidence, InsightRow


class EvidenceLedger:
    def __init__(self, events: EventLog) -> None:
        self._events = events
        self._conn = events._conn
        with events._transaction():
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS evidence "
                "(evidence_id TEXT PRIMARY KEY, content TEXT NOT NULL)"
            )

    def add(self, evidence: Evidence) -> None:
        with self._events._transaction():
            previous = self.get(evidence.evidence_id)
            if previous is not None:
                if previous != evidence:
                    raise ValueError(f"Conflicting content for evidence {evidence.evidence_id}")
                return
            self._conn.execute(
                "INSERT INTO evidence (evidence_id, content) VALUES (?, ?)",
                (evidence.evidence_id, evidence.model_dump_json()),
            )
            self._events.append(
                "evidence.added", "system", evidence.model_dump(mode="json"),
                refs=[evidence.evidence_id],
            )

    def add_rows(self, rows: list[InsightRow]) -> list[str]:
        with self._events._transaction():
            for row in rows:
                values = {key: value for key, value in row.model_dump().items()
                          if key != "day" and isinstance(value, (int, float))
                          and not isinstance(value, bool)}
                self.add(Evidence(evidence_id=row.evidence_id, kind="insight", values=values))
        return [row.evidence_id for row in rows]

    def get(self, evidence_id: str) -> Evidence | None:
        row = self._conn.execute(
            "SELECT content FROM evidence WHERE evidence_id = ?", (evidence_id,),
        ).fetchone()
        return Evidence.model_validate_json(row[0]) if row else None

    def has(self, evidence_id: str) -> bool:
        return self.get(evidence_id) is not None
