"""Paths, rules and per-project configuration. Config lives in state/, out of every agent's reach."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = ROOT / "rules" / "rules.json"
AGENTS_DIR = ROOT / "agents"
PROJECTS_DIR = ROOT / "projects"
STATE_DIR = ROOT / "state"
OUT_DIR = ".factory-out"  # agents' machine-readable outputs, gitignored inside each project

DEFAULTS: dict[str, Any] = {
    # 0 = human approves architecture, every merge and release; 1 = architecture + release;
    # 2 = release only; 3 = fully autonomous (digest only)
    "autonomy_level": 1,
    "author_model": "sonnet",
    "reviewer_model": "opus",  # D3/S9: reviewers run on a different model than authors
    "budget_usd_per_call": 10.0,
    "budget_usd_per_task": 30.0,
    "budget_usd_per_run": 300.0,
    "agent_timeout_s": 3600,
    "check_timeout_s": 1800,
}

# A4: nobody but humans (or the named owner) may change these in a project.
PROTECTED_PATHS = [
    "rules/**",
    ".github/**",
    "CODEOWNERS",
    ".gitignore",
    # agent-runtime config: a planted file here would grant the next agent more power
    ".claude/**",
    "**/.claude/**",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "CLAUDE.local.md",
    ".mcp.json",
]
# Must never exist in the working tree at all (even git-ignored).
FORBIDDEN_FILES = [".claude", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json"]
PRD_PATH = "docs/prd.md"
MAP_PATH = "docs/enforcement-map.json"
TASKS_PATH = "docs/tasks.json"
SPEC_PATH = "docs/spec.md"
SEC_REQ_PATH = "docs/security/requirements.md"


def load_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = json.loads(RULES_FILE.read_text(encoding="utf-8"))["rules"]
    return rules


def thresholds_env() -> dict[str, str]:
    """M3: thresholds reach checks only as FACTORY_* env vars, never via agent-editable files."""
    return {r["threshold"]["env"]: str(r["threshold"]["value"]) for r in load_rules() if "threshold" in r}


def threshold(env_name: str) -> float:
    return float(thresholds_env()[env_name])


@dataclass(frozen=True)
class Project:
    name: str

    @property
    def dir(self) -> Path:
        return PROJECTS_DIR / self.name

    @property
    def state(self) -> Path:
        return STATE_DIR / self.name

    @property
    def out(self) -> Path:
        return self.dir / OUT_DIR

    @cached_property
    def config(self) -> dict[str, Any]:
        path = self.state / "config.json"
        user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {**DEFAULTS, **user}

    @property
    def stop_file(self) -> Path:
        return self.state / "STOP"

    @property
    def inbox(self) -> Path:
        return self.state / "inbox.md"

    def notify(self, title: str, body: str) -> None:
        """J3: everything for the human goes to the PM inbox (and the console)."""
        self.state.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a", encoding="utf-8") as f:
            f.write(f"\n## {title}\n\n{body}\n")
        print(f"[inbox] {title}")
