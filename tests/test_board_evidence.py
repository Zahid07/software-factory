import json
from pathlib import Path

import pytest

from factory.board import Board, IllegalTransitionError
from factory.evidence import Evidence
from factory.schemas import CHANGE_REQUEST, REVIEW, TASKS, validate


def task(tid: str, deps: list[str] | None = None, *, security: bool = False) -> dict[str, object]:
    return {
        "id": tid,
        "title": tid,
        "role": "backend-dev",
        "layer": "backend",
        "description": "d",
        "req_ids": ["FR-1"],
        "paths": ["src/**"],
        "depends_on": deps or [],
        "needs_tests": True,
        "security": security,
    }


def test_board_lifecycle_and_illegal_transitions(tmp_path: Path) -> None:
    b = Board(tmp_path / "b.json")
    b.add_all([task("BE-2", ["BE-1"]), task("BE-1")])
    assert b.claimable() == ["BE-1"]
    with pytest.raises(IllegalTransitionError):
        b.move("BE-1", "merged")  # B1: cannot skip review
    for s in ["claimed", "in_progress", "ci", "review", "approved", "merged"]:
        b.move("BE-1", s)
    assert b.claimable() == ["BE-2"]
    with pytest.raises(IllegalTransitionError):
        b.move("BE-1", "halted")  # terminal
    assert Board(tmp_path / "b.json").tasks["BE-1"]["status"] == "merged"  # persisted


def test_board_rejects_cycles_unknown_deps_and_prioritises_security(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cycle"):
        Board(tmp_path / "a.json").add_all([task("A-1", ["A-2"]), task("A-2", ["A-1"])])
    with pytest.raises(ValueError, match="unknown"):
        Board(tmp_path / "c.json").add(task("A-1", ["NOPE-1"]))
    b = Board(tmp_path / "d.json")
    b.add_all([task("A-1"), task("SEC-9", security=True)])
    assert b.claimable() == ["SEC-9", "A-1"]  # S4


def test_halted_dependency_blocks(tmp_path: Path) -> None:
    b = Board(tmp_path / "b.json")
    b.add_all([task("A-1"), task("A-2", ["A-1"])])
    b.move("A-1", "halted", "x")
    assert b.blocked() == ["A-2"]
    assert b.halted_ratio() == 0.5


def test_evidence_chain_detects_tampering(tmp_path: Path) -> None:
    ev = Evidence(tmp_path / "e.jsonl")
    for i in range(3):
        ev.append("review", {"task": f"T-{i}", "verdict": "approved"})
    assert ev.verify() is None
    lines = (tmp_path / "e.jsonl").read_text().splitlines()
    record = json.loads(lines[1])
    record["verdict"] = "changes_requested"
    lines[1] = json.dumps(record)
    (tmp_path / "e.jsonl").write_text("\n".join(lines) + "\n")
    assert ev.verify() == "evidence chain broken at record 1"


def test_evidence_detects_deletion(tmp_path: Path) -> None:
    ev = Evidence(tmp_path / "e.jsonl")
    for i in range(3):
        ev.append("x", {"i": i})
    lines = (tmp_path / "e.jsonl").read_text().splitlines()
    (tmp_path / "e.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert ev.verify() is not None


def test_schemas() -> None:
    assert validate({"tasks": [task("BE-1")]}, TASKS) == []
    assert validate({"tasks": [{**task("BE-1"), "role": "wizard"}]}, TASKS)
    assert validate({"tasks": []}, TASKS)
    assert validate({"verdict": "approved", "summary": "ok", "findings": []}, REVIEW) == []
    bad = {"verdict": "approved", "summary": "ok", "findings": [{"ref": "C2", "severity": "huge", "issue": "x"}]}
    assert validate(bad, REVIEW)
    assert validate({"kind": "test_change", "target": "t", "proposal": "p", "reason": "r"}, CHANGE_REQUEST) == []
    assert validate({"verdict": True}, REVIEW)  # bool is not a string
