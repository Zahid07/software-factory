"""Task board: the only place task state lives. Illegal transitions raise (B1); claim order is S4 then B2."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

TRANSITIONS: dict[str, set[str]] = {
    "open": {"claimed"},
    "claimed": {"in_progress", "open"},
    "in_progress": {"ci", "open"},  # -> open: rejected task re-planned by the Tech Lead
    "ci": {"in_progress", "review"},
    "review": {"in_progress", "approved"},
    "approved": {"merged"},
    "merged": set(),
    "halted": {"open"},  # only after a human or Tech Lead re-plans it
}
TERMINAL = {"merged", "halted"}


class IllegalTransitionError(Exception):
    pass


class Board:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tasks: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tasks, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, task: dict[str, Any]) -> None:
        if task["id"] in self.tasks:
            raise ValueError(f"duplicate task id {task['id']}")
        unknown = [d for d in task["depends_on"] if d not in self.tasks and d != task["id"]]
        if unknown:
            raise ValueError(f"{task['id']} depends on unknown tasks {unknown}")
        self.tasks[task["id"]] = {
            **task,
            "status": "open",
            "cost_usd": 0.0,
            "ci_retries": 0,
            "review_rounds": 0,
            "change_requests": 0,
            "history": [],
        }
        self.save()

    def add_all(self, tasks: list[dict[str, Any]]) -> None:
        """Add a batch whose dependencies may reference each other; rejects cycles."""
        pending = {t["id"]: t for t in tasks}
        while pending:
            ready = [t for t in pending.values() if all(d in self.tasks or d not in pending for d in t["depends_on"])]
            if not ready:
                raise ValueError(f"dependency cycle among {sorted(pending)}")
            for t in ready:
                self.add(t)
                del pending[t["id"]]

    def move(self, task_id: str, new: str, note: str = "") -> None:
        task = self.tasks[task_id]
        old = task["status"]
        if new != "halted" and new not in TRANSITIONS[old]:
            raise IllegalTransitionError(f"{task_id}: {old} -> {new} is not allowed (B1)")
        if new == "halted" and old in TERMINAL:
            raise IllegalTransitionError(f"{task_id}: {old} is terminal")
        task["status"] = new
        task["history"].append({"at": datetime.now(UTC).isoformat(), "from": old, "to": new, "note": note})
        self.save()

    def claimable(self) -> list[str]:
        """B2: deps merged. S4: security tasks first. Then by id for determinism."""
        ready = [
            t
            for t in self.tasks.values()
            if t["status"] == "open" and all(self.tasks[d]["status"] == "merged" for d in t["depends_on"])
        ]
        ready.sort(key=lambda t: (not t.get("security", False), t["id"]))
        return [t["id"] for t in ready]

    def blocked(self) -> list[str]:
        """Open tasks that can never run because a dependency halted."""
        return [
            t["id"]
            for t in self.tasks.values()
            if t["status"] == "open" and any(self.tasks[d]["status"] == "halted" for d in t["depends_on"])
        ]

    def halted_ratio(self) -> float:
        halted = sum(1 for t in self.tasks.values() if t["status"] == "halted")
        return halted / max(len(self.tasks), 1)

    def done(self) -> bool:
        return all(t["status"] in TERMINAL for t in self.tasks.values())
