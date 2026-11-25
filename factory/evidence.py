"""Append-only, hash-chained evidence log (H1, H2). Lives in state/, outside every agent's working dir."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

GENESIS = "0" * 64
# ponytail: a full rewrite of the file can recompute the chain; anchor the head hash externally
# (e.g. signed git tag or a write-once bucket) when evidence must survive a hostile human.


def _digest(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Evidence:
    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out = [json.loads(line) for line in lines if line.strip()]
        return [r for r in out if kind is None or r["kind"] == kind]

    def append(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.records()
        record: dict[str, Any] = {
            "seq": len(existing),
            "at": datetime.now(UTC).isoformat(),
            "kind": kind,
            "prev": existing[-1]["hash"] if existing else GENESIS,
            **data,
        }
        record["hash"] = _digest(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:  # append-only by construction
            f.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def verify(self) -> str | None:
        """Return None if the chain is intact, else a description of the first break."""
        prev = GENESIS
        for i, record in enumerate(self.records()):
            if record.get("seq") != i or record.get("prev") != prev or record.get("hash") != _digest(record):
                return f"evidence chain broken at record {i}"
            prev = record["hash"]
        return None
