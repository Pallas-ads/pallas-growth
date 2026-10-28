"""One SQLite workspace for the deterministic collaboration harness."""

import sqlite3
from pathlib import Path

from pgt.harness.approvals import Approvals
from pgt.harness.artifacts import ArtifactStore
from pgt.harness.events import EventLog
from pgt.harness.facts import FactStore
from pgt.harness.ledger import EvidenceLedger


class Workspace:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self.events = EventLog(self._conn)
        self.artifacts = ArtifactStore(self.events)
        self.facts = FactStore(self.events, self.artifacts)
        self.ledger = EvidenceLedger(self.events)
        self.approvals = Approvals(self.events, self.artifacts)

    def close(self) -> None:
        self._conn.close()
